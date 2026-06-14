"""
Kubernetes MCP Server – Main Entry Point

Exposes Kubernetes multi-cluster operations, RCA, and self-healing as MCP
tools via the FastMCP framework.  Supports streamable HTTP and stdio
transports.
"""

from __future__ import annotations

import asyncio
import json
import logging
import warnings
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import urllib3
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp.server.fastmcp import FastMCP

from src.config import settings
from src.connectors.cluster import connector
from src.models import ClusterConfig, K8sFlavor
from src.prompts.k8s_prompts import get_prompt
from src.resources.cluster_resources import get_cluster_health_resource, get_cluster_list
from src.tools import efficient_ops, k8s_ops
from src.tools.rca import run_cluster_rca, run_namespace_rca, run_pod_rca
from src.tools.self_heal import execute_heal_plan, generate_heal_plan, quick_heal

# Suppress noisy InsecureRequestWarning when skip_tls_verify is enabled
warnings.filterwarnings("ignore", category=urllib3.exceptions.InsecureRequestWarning)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


# ── Lifespan ─────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Initialise cluster connections before the server starts accepting
    requests and tear them down on shutdown."""
    logger.info("Initialising cluster connections …")
    for cfg in settings.clusters:
        try:
            connector.register(cfg)
        except Exception as e:
            logger.warning("  ✗ %s – failed to register: %s", cfg.name, e)

    # Run a lightweight reachability check (version only) in background threads.
    # The full health_check (lists pods/nodes/events) is too expensive for
    # startup — it can take minutes on large production clusters.
    async def _check_cluster(name: str) -> None:
        try:
            from kubernetes import client as k8s_client

            def _fast_ping() -> str:
                api = connector.get_client(name)
                ver = k8s_client.VersionApi(api).get_code()
                return f"v{ver.major}.{ver.minor}"

            version = await asyncio.to_thread(_fast_ping)
            logger.info(
                "  ✓ %s (%s) – reachable (%s)",
                name,
                connector.get_config(name).flavor.value,
                version,
            )
        except Exception as e:
            logger.warning("  ✗ %s – unreachable: %s", name, e)

    # Fire all reachability checks concurrently (non-blocking)
    if connector.clusters:
        await asyncio.gather(
            *[_check_cluster(name) for name in connector.clusters],
            return_exceptions=True,
        )

    logger.info("%d cluster(s) registered.", len(connector.clusters))

    # Pre-warm the low-level tool cache so that the first call_tool request
    # does not trigger the "Tool 'X' not listed, no validation" warning.
    try:
        from mcp import types as mcp_types

        ll_server = server._mcp_server  # type: ignore[attr-access]
        handler = ll_server.request_handlers.get(mcp_types.ListToolsRequest)
        if handler:
            await handler(None)
            logger.info(
                "Tool cache pre-warmed with %d tool(s).",
                len(ll_server._tool_cache),
            )
    except Exception as exc:
        logger.debug("Could not pre-warm tool cache: %s", exc)

    yield {"connector": connector}
    logger.info("Shutting down Kubernetes MCP Server.")


# ── Server ───────────────────────────────────────────────────────────────────

mcp = FastMCP(
    "Kubernetes MCP Server",
    instructions=(
        "Multi-cluster Kubernetes MCP server with RCA and self-healing. "
        "Use cluster management tools to list and inspect clusters, Kubernetes "
        "operations tools to manage workloads, RCA tools to diagnose issues, "
        "and self-heal tools to remediate problems.\n\n"
        "To stay token-efficient, PREFER the consolidated read tools over chaining "
        "granular calls:\n"
        "• cluster_overview — cluster posture in one call\n"
        "• namespace_overview — triage a namespace in one call\n"
        "• get_pod_context — pod status + events + logs in one call (use before get_pod/get_pod_logs)\n"
        "• get_deployment_context — deployment + rollout + pods + events in one call\n"
        "• batch_read — run many read operations in a single round-trip\n"
        "• project_resource — return only the specific fields you need\n"
        "Granular list_*/get_* tools accept selectors and a `limit`; narrow with "
        "label/field selectors instead of listing everything, and request "
        "response_format='detailed' only when you actually need the full object."
    ),
    lifespan=lifespan,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CLUSTER MANAGEMENT TOOLS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@mcp.tool(structured_output=False)
def list_clusters() -> list[dict]:
    """List all registered Kubernetes clusters with their flavor and API server."""
    return get_cluster_list()


@mcp.tool(structured_output=False)
def cluster_health(cluster: str) -> dict:
    """Check the health of a registered cluster. Returns reachability, version, node and pod counts."""
    return get_cluster_health_resource(cluster)


@mcp.tool(structured_output=False)
def register_cluster(
    name: str,
    api_server: str = "",
    sa_token: str | None = None,
    flavor: str = "vanilla",
    skip_tls_verify: bool = False,
    ca_cert: str | None = None,
    proxy_url: str | None = None,
) -> dict:
    """Dynamically register a new cluster at runtime.

    For direct API server access, provide api_server + sa_token.
    For kubectl proxy / API gateway mode, provide proxy_url instead
    (e.g. http://localhost:8001). No token is needed with proxy mode.
    """
    cfg = ClusterConfig(
        name=name,
        flavor=K8sFlavor(flavor),
        api_server=api_server,
        sa_token=sa_token,
        skip_tls_verify=skip_tls_verify,
        ca_cert=ca_cert,
        proxy_url=proxy_url,
    )
    connector.register(cfg)
    health = connector.health_check(name)
    return {"registered": True, "cluster": name, "healthy": health.reachable}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  NAMESPACE TOOLS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@mcp.tool(structured_output=False)
def list_namespaces(cluster: str, label_selector: str = "",
                    field_selector: str = "") -> list[dict]:
    """List all namespaces in a cluster with labels, annotations, and status.

    Supports filtering by label_selector (e.g. 'env=prod') and
    field_selector (e.g. 'metadata.name=kube-system,status.phase=Active')."""
    return k8s_ops.list_namespaces(cluster, label_selector, field_selector)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  POD TOOLS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@mcp.tool(structured_output=False)
def list_pods(cluster: str, namespace: str = "default",
              label_selector: str = "", field_selector: str = "") -> list[dict]:
    """List pods with full metadata including labels, annotations, images, resource requests/limits, QoS class, and status.

    Supports filtering:
    - label_selector: e.g. 'app=myapp,env=prod' or 'app in (myapp,yourapp)'
    - field_selector: e.g. 'status.phase=Running', 'spec.nodeName=node1'"""
    return k8s_ops.list_pods(cluster, namespace, label_selector, field_selector)


@mcp.tool(structured_output=False)
def get_pod(cluster: str, name: str, namespace: str = "default") -> dict:
    """Get detailed information about a specific pod (full spec/status)."""
    return k8s_ops.get_pod(cluster, namespace, name)


@mcp.tool(structured_output=False)
def get_pod_logs(
    cluster: str,
    name: str,
    namespace: str = "default",
    container: str | None = None,
    tail_lines: int = 100,
    previous: bool = False,
) -> str:
    """Retrieve logs from a pod. Optionally specify container, number of tail lines, or previous instance."""
    return k8s_ops.get_pod_logs(cluster, namespace, name, container, tail_lines, previous)


@mcp.tool(structured_output=False)
def delete_pod(cluster: str, name: str, namespace: str = "default") -> dict:
    """Delete a pod. Useful for forcing a restart when managed by a controller."""
    if settings.read_only:
        return {"error": "Server is in read-only mode."}
    return k8s_ops.delete_pod(cluster, namespace, name)


@mcp.tool(structured_output=False)
def restart_pod(cluster: str, name: str, namespace: str = "default") -> dict:
    """Restart a pod by deleting it (the controller will recreate it)."""
    if settings.read_only:
        return {"error": "Server is in read-only mode."}
    return k8s_ops.restart_pod(cluster, namespace, name)


@mcp.tool(structured_output=False)
def exec_pod(cluster: str, name: str, command: list[str],
             namespace: str = "default", container: str | None = None) -> dict:
    """Execute a command inside a running pod. Returns stdout/stderr output.

    Example: exec_pod(cluster='mycluster', name='nginx-pod', command=['ls', '-la', '/tmp'])"""
    return k8s_ops.exec_pod(cluster, namespace, name, command, container)


@mcp.tool(structured_output=False)
def top_pods(cluster: str, namespace: str = "") -> list[dict]:
    """Get resource usage (CPU/memory) for pods from the Kubernetes Metrics API.
    Requires metrics-server to be installed in the cluster."""
    return k8s_ops.top_pods(cluster, namespace)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DEPLOYMENT TOOLS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@mcp.tool(structured_output=False)
def list_deployments(cluster: str, namespace: str = "default",
                     label_selector: str = "") -> list[dict]:
    """List deployments with full metadata including labels, annotations, strategy, images, and conditions.

    Supports label_selector filtering (e.g. 'app=myapp')."""
    return k8s_ops.list_deployments(cluster, namespace, label_selector)


@mcp.tool(structured_output=False)
def get_deployment(cluster: str, name: str, namespace: str = "default") -> dict:
    """Get detailed information about a specific deployment (full spec/status)."""
    return k8s_ops.get_deployment(cluster, namespace, name)


@mcp.tool(structured_output=False)
def scale_deployment(cluster: str, name: str, replicas: int, namespace: str = "default") -> dict:
    """Scale a deployment to the desired number of replicas."""
    if settings.read_only:
        return {"error": "Server is in read-only mode."}
    return k8s_ops.scale_deployment(cluster, namespace, name, replicas)


@mcp.tool(structured_output=False)
def restart_deployment(cluster: str, name: str, namespace: str = "default") -> dict:
    """Perform a rollout restart on a deployment."""
    if settings.read_only:
        return {"error": "Server is in read-only mode."}
    return k8s_ops.restart_deployment(cluster, namespace, name)


@mcp.tool(structured_output=False)
def get_rollout_status(cluster: str, name: str, namespace: str = "default") -> dict:
    """Get the rollout status of a deployment (similar to kubectl rollout status).
    Shows progress, conditions, and replica counts."""
    return k8s_ops.get_rollout_status(cluster, namespace, name)


@mcp.tool(structured_output=False)
def rollback_deployment(cluster: str, name: str, namespace: str = "default",
                        revision: int = 0) -> dict:
    """Rollback a deployment to a previous revision.
    If revision=0, rolls back to the immediately previous revision.
    Otherwise specify the exact revision number to roll back to."""
    if settings.read_only:
        return {"error": "Server is in read-only mode."}
    return k8s_ops.rollback_deployment(cluster, namespace, name, revision)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  NODE TOOLS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@mcp.tool(structured_output=False)
def list_nodes(cluster: str, label_selector: str = "") -> list[dict]:
    """List all nodes with enriched info: capacity, allocatable resources, taints, conditions, IPs, and labels.

    Supports label_selector filtering (e.g. 'node-role.kubernetes.io/worker=')."""
    return k8s_ops.list_nodes(cluster, label_selector)


@mcp.tool(structured_output=False)
def top_nodes(cluster: str) -> list[dict]:
    """Get resource usage (CPU/memory) for all nodes from the Kubernetes Metrics API.
    Requires metrics-server to be installed in the cluster."""
    return k8s_ops.top_nodes(cluster)


@mcp.tool(structured_output=False)
def cordon_node(cluster: str, node_name: str) -> dict:
    """Mark a node as unschedulable (cordon)."""
    if settings.read_only:
        return {"error": "Server is in read-only mode."}
    return k8s_ops.cordon_node(cluster, node_name)


@mcp.tool(structured_output=False)
def uncordon_node(cluster: str, node_name: str) -> dict:
    """Mark a node as schedulable again (uncordon)."""
    if settings.read_only:
        return {"error": "Server is in read-only mode."}
    return k8s_ops.uncordon_node(cluster, node_name)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  EVENT & SERVICE TOOLS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@mcp.tool(structured_output=False)
def list_events(cluster: str, namespace: str = "default",
                event_type: str = "", field_selector: str = "",
                limit: int = 100) -> list[dict]:
    """List recent events with enriched metadata.

    Supports filtering:
    - event_type: 'Warning' or 'Normal'
    - field_selector: e.g. 'involvedObject.name=mypod,involvedObject.kind=Pod'
    - limit: max events to return (default 100)"""
    return k8s_ops.list_events(cluster, namespace, event_type, field_selector, limit)


@mcp.tool(structured_output=False)
def list_services(cluster: str, namespace: str = "default",
                  label_selector: str = "") -> list[dict]:
    """List services with full metadata including selectors, external IPs, labels, and annotations.

    Supports label_selector filtering (e.g. 'app=myapp')."""
    return k8s_ops.list_services(cluster, namespace, label_selector)


@mcp.tool(structured_output=False)
def list_configmaps(cluster: str, namespace: str = "default",
                    label_selector: str = "") -> list[dict]:
    """List ConfigMaps with data keys, labels, and annotations.

    Supports label_selector filtering."""
    return k8s_ops.list_configmaps(cluster, namespace, label_selector)


@mcp.tool(structured_output=False)
def get_configmap(cluster: str, name: str, namespace: str = "default") -> dict:
    """Get a specific ConfigMap's full data, labels, and annotations."""
    return k8s_ops.get_configmap(cluster, namespace, name)


@mcp.tool(structured_output=False)
def list_secrets(cluster: str, namespace: str = "default",
                 label_selector: str = "") -> list[dict]:
    """List secrets (names, types, data keys, labels – no actual secret data exposed).

    Supports label_selector filtering."""
    return k8s_ops.list_secrets(cluster, namespace, label_selector)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GENERIC RESOURCE TOOLS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@mcp.tool(structured_output=False)
def get_resource(
    cluster: str,
    api_version: str,
    kind: str,
    name: str,
    namespace: str | None = None,
) -> dict:
    """Get any Kubernetes resource by apiVersion, kind, name, and optional namespace.
    Returns the full resource spec/status as YAML-like dict.

    Common apiVersion/kind: v1/Pod, v1/Service, apps/v1/Deployment, networking.k8s.io/v1/Ingress"""
    return k8s_ops.get_resource(cluster, api_version, kind, name, namespace)


@mcp.tool(structured_output=False)
def list_resources(
    cluster: str,
    api_version: str,
    kind: str,
    namespace: str | None = None,
    label_selector: str = "",
    field_selector: str = "",
) -> list[dict]:
    """List any Kubernetes resources by apiVersion and kind.

    Supports label_selector and field_selector filtering.
    Common apiVersion/kind: v1/Pod, apps/v1/Deployment, batch/v1/Job, networking.k8s.io/v1/Ingress"""
    return k8s_ops.list_resources(cluster, api_version, kind, namespace, label_selector, field_selector)


@mcp.tool(structured_output=False)
def create_or_update_resource(cluster: str, manifest: dict) -> dict:
    """Create or update a Kubernetes resource from a full manifest dict (apiVersion, kind, metadata, spec)."""
    if settings.read_only:
        return {"error": "Server is in read-only mode."}
    return k8s_ops.create_or_update_resource(cluster, manifest)


@mcp.tool(structured_output=False)
def delete_resource(
    cluster: str,
    api_version: str,
    kind: str,
    name: str,
    namespace: str | None = None,
) -> dict:
    """Delete any Kubernetes resource by apiVersion, kind, name, and optional namespace."""
    if settings.read_only:
        return {"error": "Server is in read-only mode."}
    return k8s_ops.delete_resource(cluster, api_version, kind, name, namespace)


@mcp.tool(structured_output=False)
def describe_resource(
    cluster: str,
    api_version: str,
    kind: str,
    name: str,
    namespace: str | None = None,
) -> dict:
    """Get a rich description of any Kubernetes resource combining its full spec/status
    with related events (similar to 'kubectl describe').

    Common apiVersion/kind: v1/Pod, apps/v1/Deployment, v1/Service, v1/Node"""
    return k8s_ops.describe_resource(cluster, api_version, kind, name, namespace)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CUSTOM RESOURCE / CRD TOOLS
#
#  Discover and query CustomResourceDefinitions and their instances. The CRD is
#  resolved automatically so you can fetch CRs by Kind without knowing the
#  apiVersion, plural, or scope.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@mcp.tool(structured_output=False)
def list_crds(cluster: str, label_selector: str = "") -> list[dict]:
    """List CustomResourceDefinitions with group, kind, plural, scope, served/
    storage versions, and short names — everything needed to then query the CRs."""
    return k8s_ops.list_crds(cluster, label_selector)


@mcp.tool(structured_output=False)
def list_api_resources(cluster: str, api_version: str = "") -> list[dict]:
    """Discover served API resources (like 'kubectl api-resources').

    With api_version (e.g. 'cert-manager.io/v1') lists that group's kinds; with
    no argument enumerates every served group at its preferred version. Use this
    to find which custom and built-in kinds exist before querying them."""
    return k8s_ops.list_api_resources(cluster, api_version)


@mcp.tool(structured_output=False)
def list_custom_resources(
    cluster: str,
    kind: str,
    namespace: str | None = None,
    label_selector: str = "",
    limit: int = 0,
) -> list[dict]:
    """List Custom Resources by Kind alone (e.g. kind='Certificate').

    The CRD is looked up to resolve its group, version, plural, and scope, so you
    don't need the apiVersion. For namespaced kinds, omit namespace to list
    across all namespaces."""
    return k8s_ops.list_custom_resources(cluster, kind, namespace, label_selector, limit)


@mcp.tool(structured_output=False)
def get_custom_resource(
    cluster: str,
    kind: str,
    name: str,
    namespace: str | None = None,
) -> dict:
    """Get a single Custom Resource by Kind and name (CRD resolved automatically).

    Example: get_custom_resource(cluster='c', kind='Certificate', name='my-cert',
    namespace='default')."""
    return k8s_ops.get_custom_resource(cluster, kind, name, namespace)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  STATEFULSET TOOLS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@mcp.tool(structured_output=False)
def list_statefulsets(cluster: str, namespace: str = "default",
                     label_selector: str = "") -> list[dict]:
    """List StatefulSets with replicas, images, service name, and update strategy.

    Supports label_selector filtering."""
    return k8s_ops.list_statefulsets(cluster, namespace, label_selector)


@mcp.tool(structured_output=False)
def get_statefulset(cluster: str, name: str, namespace: str = "default") -> dict:
    """Get detailed information about a specific StatefulSet (full spec/status)."""
    return k8s_ops.get_statefulset(cluster, namespace, name)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DAEMONSET TOOLS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@mcp.tool(structured_output=False)
def list_daemonsets(cluster: str, namespace: str = "default",
                   label_selector: str = "") -> list[dict]:
    """List DaemonSets with scheduled/ready counts, images, and node selectors.

    Supports label_selector filtering."""
    return k8s_ops.list_daemonsets(cluster, namespace, label_selector)


@mcp.tool(structured_output=False)
def get_daemonset(cluster: str, name: str, namespace: str = "default") -> dict:
    """Get detailed information about a specific DaemonSet (full spec/status)."""
    return k8s_ops.get_daemonset(cluster, namespace, name)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  JOB / CRONJOB TOOLS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@mcp.tool(structured_output=False)
def list_jobs(cluster: str, namespace: str = "default",
              label_selector: str = "") -> list[dict]:
    """List Jobs with completion status, active/failed counts, and duration.

    Supports label_selector filtering."""
    return k8s_ops.list_jobs(cluster, namespace, label_selector)


@mcp.tool(structured_output=False)
def list_cronjobs(cluster: str, namespace: str = "default",
                  label_selector: str = "") -> list[dict]:
    """List CronJobs with schedule, suspend status, last run time, and active job count.

    Supports label_selector filtering."""
    return k8s_ops.list_cronjobs(cluster, namespace, label_selector)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  INGRESS TOOLS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@mcp.tool(structured_output=False)
def list_ingresses(cluster: str, namespace: str = "default",
                   label_selector: str = "") -> list[dict]:
    """List Ingresses with rules (hosts/paths/backends), TLS config, and load balancer IPs.

    Supports label_selector filtering."""
    return k8s_ops.list_ingresses(cluster, namespace, label_selector)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PVC TOOLS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@mcp.tool(structured_output=False)
def list_pvcs(cluster: str, namespace: str = "default",
              label_selector: str = "") -> list[dict]:
    """List PersistentVolumeClaims with status, capacity, access modes, and storage class.

    Supports label_selector filtering."""
    return k8s_ops.list_pvcs(cluster, namespace, label_selector)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HPA TOOLS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@mcp.tool(structured_output=False)
def list_hpas(cluster: str, namespace: str = "default",
              label_selector: str = "") -> list[dict]:
    """List HorizontalPodAutoscalers with target reference, min/max/current replicas,
    and current/target metrics.

    Supports label_selector filtering."""
    return k8s_ops.list_hpas(cluster, namespace, label_selector)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  TOKEN-EFFICIENT / CONSOLIDATED TOOLS
#
#  These fold several round-trips into one high-signal call and let the agent
#  control verbosity. Prefer them over chaining the granular tools above.
#  See docs/TOOL_EFFICIENCY.md for the design and the research it is based on.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@mcp.tool(structured_output=False)
def cluster_overview(cluster: str) -> dict:
    """One-call cluster posture: node readiness, problem nodes, namespace count,
    and the most recent cluster-wide warning events.

    Use this first instead of separately calling list_nodes + list_namespaces +
    list_events. It is intentionally light (no all-pod listing)."""
    return efficient_ops.cluster_overview(cluster)


@mcp.tool(structured_output=False)
def namespace_overview(cluster: str, namespace: str = "default") -> dict:
    """One-call namespace triage: pod phase counts, the specific problem pods,
    under-provisioned deployments, unbound PVCs, service count, and recent
    warnings.

    Replaces list_pods + list_deployments + list_events + list_pvcs +
    list_services followed by manual filtering."""
    return efficient_ops.namespace_overview(cluster, namespace)


@mcp.tool(structured_output=False)
def get_pod_context(
    cluster: str,
    name: str,
    namespace: str = "default",
    tail_lines: int = 50,
    include_logs: bool = True,
    response_format: str = "concise",
) -> dict:
    """Everything needed to triage one pod in a single call: status, per-container
    states with failure reasons/exit codes, owner refs, conditions, scoped
    events, and log tails (previous logs included automatically for restarting
    containers).

    Replaces get_pod + describe_resource + get_pod_logs + list_events. Set
    response_format='detailed' to also include the full raw pod object."""
    return efficient_ops.get_pod_context(
        cluster, namespace, name, tail_lines, include_logs, response_format
    )


@mcp.tool(structured_output=False)
def get_deployment_context(
    cluster: str,
    name: str,
    namespace: str = "default",
    response_format: str = "concise",
) -> dict:
    """Deployment health in one call: summary, rollout progress/conditions, the
    pods it owns (concise), and recent events.

    Replaces get_deployment + get_rollout_status + list_pods + list_events. Set
    response_format='detailed' to also include the full raw deployment object."""
    return efficient_ops.get_deployment_context(cluster, namespace, name, response_format)


@mcp.tool(structured_output=False)
def project_resource(
    cluster: str,
    api_version: str,
    kind: str,
    name: str,
    fields: list[str],
    namespace: str | None = None,
) -> dict:
    """Fetch any resource but return ONLY the requested dotted-path fields,
    keeping large objects out of context.

    Example fields: ["status.phase", "spec.nodeName",
    "status.containerStatuses[0].restartCount"]."""
    return efficient_ops.project_resource(cluster, api_version, kind, name, fields, namespace)


@mcp.tool(structured_output=False)
def batch_read(cluster: str, operations: list[dict]) -> list[dict]:
    """Run several READ-ONLY operations against one cluster in a single round-trip
    instead of chaining individual tool calls.

    Each operation is {"op": "<name>", "args": {...}} where args are the tool's
    keyword arguments excluding 'cluster'. Example:
      [{"op": "namespace_overview", "args": {"namespace": "prod"}},
       {"op": "list_nodes", "args": {}},
       {"op": "get_pod_context", "args": {"namespace": "prod", "name": "api-0"}}]
    A failure in one operation does not abort the others."""
    return efficient_ops.batch_read(cluster, operations)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  RCA TOOLS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@mcp.tool(structured_output=False)
def cluster_rca(cluster: str) -> dict:
    """Run root-cause analysis across an entire cluster. Returns a structured
    report with conditions, probable root cause, and recommended actions."""
    if not settings.enable_rca:
        return {"error": "RCA feature is disabled."}
    report = run_cluster_rca(cluster)
    return report.model_dump()


@mcp.tool(structured_output=False)
def pod_rca(cluster: str, name: str, namespace: str = "default") -> dict:
    """Run root-cause analysis focused on a specific pod. Returns conditions,
    probable root cause, recommended actions, and recent log tail."""
    if not settings.enable_rca:
        return {"error": "RCA feature is disabled."}
    report = run_pod_rca(cluster, namespace, name)
    return report.model_dump()


@mcp.tool(structured_output=False)
def namespace_rca(cluster: str, namespace: str) -> dict:
    """Run root-cause analysis across all workloads in a namespace."""
    if not settings.enable_rca:
        return {"error": "RCA feature is disabled."}
    report = run_namespace_rca(cluster, namespace)
    return report.model_dump()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SELF-HEAL TOOLS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@mcp.tool(structured_output=False)
def heal_plan(cluster: str, dry_run: bool = True, requires_approval: bool = True) -> dict:
    """Generate a self-healing plan from an RCA scan.  By default runs as
    dry-run with approval required.  Returns the plan with proposed actions."""
    if not settings.enable_self_heal:
        return {"error": "Self-heal feature is disabled."}
    rca_report = run_cluster_rca(cluster)
    plan = generate_heal_plan(rca_report, dry_run=dry_run, requires_approval=requires_approval)
    return plan.model_dump()


@mcp.tool(structured_output=False)
def heal_execute(cluster: str, plan_json: str, force: bool = False) -> list[dict]:
    """Execute a previously generated healing plan.  Pass force=True to skip
    the approval gate.  plan_json is the JSON string of the HealPlan."""
    if not settings.enable_self_heal:
        return [{"error": "Self-heal feature is disabled."}]
    if settings.read_only:
        return [{"error": "Server is in read-only mode."}]

    from src.models import HealPlan

    plan = HealPlan.model_validate_json(plan_json)
    results = execute_heal_plan(cluster, plan, force=force)
    return [r.model_dump() for r in results]


@mcp.tool(structured_output=False)
def heal_quick(
    cluster: str,
    action_type: str,
    resource: str,
    namespace: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Execute a single healing action directly (bypass plan/approval).
    action_type: restart_pod | rollout_restart | scale_deployment |
                 cordon_node | drain_node | uncordon_node"""
    if not settings.enable_self_heal:
        return {"error": "Self-heal feature is disabled."}
    if settings.read_only and not dry_run:
        return {"error": "Server is in read-only mode."}
    result = quick_heal(cluster, action_type, resource, namespace, dry_run=dry_run)
    return result.model_dump()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MCP PROMPTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@mcp.prompt()
def cluster_health_check(cluster: str) -> str:
    """Prompt template for a cluster-wide health review."""
    return get_prompt("cluster_health_check", cluster=cluster)


@mcp.prompt()
def pod_troubleshoot(cluster: str, namespace: str, pod: str) -> str:
    """Prompt template for debugging a specific pod."""
    return get_prompt("pod_troubleshoot", cluster=cluster, namespace=namespace, pod=pod)


@mcp.prompt()
def self_heal_workflow(cluster: str) -> str:
    """Prompt template for guided self-healing workflow."""
    return get_prompt("self_heal_workflow", cluster=cluster)


@mcp.prompt()
def namespace_review(cluster: str, namespace: str) -> str:
    """Prompt template for reviewing a namespace."""
    return get_prompt("namespace_review", cluster=cluster, namespace=namespace)


@mcp.prompt()
def multi_cluster_overview() -> str:
    """Prompt template for a multi-cluster dashboard."""
    return get_prompt("multi_cluster_overview")


@mcp.prompt()
def incident_response(alert_message: str) -> str:
    """Prompt template for incident response."""
    return get_prompt("incident_response", alert_message=alert_message)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MCP RESOURCES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@mcp.resource("k8s://clusters")
def clusters_resource() -> str:
    """Resource listing all registered clusters."""
    return json.dumps(get_cluster_list(), default=str)


@mcp.resource("k8s://clusters/{cluster}/health")
def cluster_health_resource(cluster: str) -> str:
    """Resource returning health info for a specific cluster."""
    return json.dumps(get_cluster_health_resource(cluster), default=str)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ENTRY POINT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def health_endpoint(request: Request) -> JSONResponse:
    """Health check endpoint for Kubernetes probes."""
    return JSONResponse({"status": "ok"})


def main():
    """Run the MCP server."""
    import uvicorn

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logger.info(
        "Starting Kubernetes MCP Server on %s:%d (transport=%s)",
        settings.host,
        settings.port,
        settings.transport,
    )

    # Configure host/port/path via MCP settings (run() only accepts transport)
    mcp.settings.host = settings.host
    mcp.settings.port = settings.port

    if settings.transport in ("streamable-http", "sse"):
        # Disable DNS rebinding protection for production (behind Istio gateway)
        if mcp.settings.transport_security:
            mcp.settings.transport_security.enable_dns_rebinding_protection = False

        # Add health endpoint as a custom route in the MCP app
        mcp._custom_starlette_routes = [
            Route("/health", health_endpoint, methods=["GET"]),
        ]

        # Use the MCP app directly – it handles /mcp path internally
        # and properly manages its own lifespan (session manager task group)
        app = mcp.streamable_http_app()
        uvicorn.run(app, host=settings.host, port=settings.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
