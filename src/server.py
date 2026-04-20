"""
Kubernetes MCP Server – Main Entry Point

Exposes Kubernetes multi-cluster operations, RCA, and self-healing as MCP
tools via the FastMCP framework.  Supports streamable HTTP and stdio
transports.
"""

from __future__ import annotations

import json
import logging
import warnings
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import urllib3

from mcp.server.fastmcp import FastMCP

from src.config import settings
from src.connectors.cluster import connector
from src.models import ClusterConfig, K8sFlavor
from src.prompts.k8s_prompts import get_prompt
from src.resources.cluster_resources import get_cluster_health_resource, get_cluster_list
from src.tools import k8s_ops
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
            health = connector.health_check(cfg.name)
            logger.info(
                "  ✓ %s (%s) – %s",
                cfg.name,
                cfg.flavor.value,
                "healthy" if health.reachable else "UNREACHABLE",
            )
        except Exception as e:
            logger.warning("  ✗ %s – failed to register: %s", cfg.name, e)

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
        "and self-heal tools to remediate problems."
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
def list_namespaces(cluster: str) -> list[dict]:
    """List all namespaces in a cluster."""
    return k8s_ops.list_namespaces(cluster)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  POD TOOLS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@mcp.tool(structured_output=False)
def list_pods(cluster: str, namespace: str = "default") -> list[dict]:
    """List pods in a namespace. Returns name, phase, restarts, and node for each pod."""
    return k8s_ops.list_pods(cluster, namespace)


@mcp.tool(structured_output=False)
def get_pod(cluster: str, name: str, namespace: str = "default") -> dict:
    """Get detailed information about a specific pod."""
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DEPLOYMENT TOOLS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@mcp.tool(structured_output=False)
def list_deployments(cluster: str, namespace: str = "default") -> list[dict]:
    """List deployments in a namespace with replica status."""
    return k8s_ops.list_deployments(cluster, namespace)


@mcp.tool(structured_output=False)
def get_deployment(cluster: str, name: str, namespace: str = "default") -> dict:
    """Get detailed information about a specific deployment."""
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  NODE TOOLS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@mcp.tool(structured_output=False)
def list_nodes(cluster: str) -> list[dict]:
    """List all nodes in a cluster with status, roles, and resource info."""
    return k8s_ops.list_nodes(cluster)


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
def list_events(cluster: str, namespace: str = "default") -> list[dict]:
    """List recent events in a namespace."""
    return k8s_ops.list_events(cluster, namespace)


@mcp.tool(structured_output=False)
def list_services(cluster: str, namespace: str = "default") -> list[dict]:
    """List services in a namespace."""
    return k8s_ops.list_services(cluster, namespace)


@mcp.tool(structured_output=False)
def list_configmaps(cluster: str, namespace: str = "default") -> list[dict]:
    """List ConfigMaps in a namespace."""
    return k8s_ops.list_configmaps(cluster, namespace)


@mcp.tool(structured_output=False)
def get_configmap(cluster: str, name: str, namespace: str = "default") -> dict:
    """Get a specific ConfigMap's data."""
    return k8s_ops.get_configmap(cluster, namespace, name)


@mcp.tool(structured_output=False)
def list_secrets(cluster: str, namespace: str = "default") -> list[dict]:
    """List secrets in a namespace (names and types only, no data)."""
    return k8s_ops.list_secrets(cluster, namespace)


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
    """Get any Kubernetes resource by apiVersion, kind, name, and optional namespace."""
    return k8s_ops.get_resource(cluster, api_version, kind, name, namespace)


@mcp.tool(structured_output=False)
def list_resources(
    cluster: str,
    api_version: str,
    kind: str,
    namespace: str | None = None,
) -> list[dict]:
    """List Kubernetes resources by apiVersion and kind, optionally in a namespace."""
    return k8s_ops.list_resources(cluster, api_version, kind, namespace)


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


def main():
    """Run the MCP server."""
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

    if settings.transport == "streamable-http":
        mcp.run(transport="streamable-http")
    elif settings.transport == "sse":
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
