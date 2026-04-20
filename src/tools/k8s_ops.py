"""
Kubernetes MCP Server – Core Kubernetes Operations

Provides basic CRUD and inspection operations on Kubernetes resources.
"""

from __future__ import annotations

import logging
from typing import Any

from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

from src.connectors.cluster import connector

logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _api(cluster: str) -> k8s_client.ApiClient:
    return connector.get_client(cluster)


def _core(cluster: str) -> k8s_client.CoreV1Api:
    return k8s_client.CoreV1Api(_api(cluster))


def _apps(cluster: str) -> k8s_client.AppsV1Api:
    return k8s_client.AppsV1Api(_api(cluster))


def _batch(cluster: str) -> k8s_client.BatchV1Api:
    return k8s_client.BatchV1Api(_api(cluster))


def _custom(cluster: str) -> k8s_client.CustomObjectsApi:
    return k8s_client.CustomObjectsApi(_api(cluster))


def _serialize(obj: Any) -> dict | list | str:
    """Best-effort serialize a K8s API object to dict."""
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return str(obj)


# ── Namespace Operations ─────────────────────────────────────────────────────


def list_namespaces(cluster: str) -> list[dict[str, str]]:
    """List all namespaces in a cluster."""
    ns_list = _core(cluster).list_namespace()
    return [{"name": ns.metadata.name, "status": ns.status.phase if ns.status else "Unknown"} for ns in ns_list.items]


# ── Pod Operations ───────────────────────────────────────────────────────────


def list_pods(cluster: str, namespace: str = "") -> list[dict[str, Any]]:
    """List pods. If namespace is empty, list across all namespaces."""
    v1 = _core(cluster)
    if namespace:
        pod_list = v1.list_namespaced_pod(namespace)
    else:
        pod_list = v1.list_pod_for_all_namespaces()

    results = []
    for pod in pod_list.items:
        phase = pod.status.phase if pod.status else "Unknown"
        restarts = 0
        if pod.status and pod.status.container_statuses:
            restarts = sum(cs.restart_count for cs in pod.status.container_statuses)

        results.append({
            "name": pod.metadata.name,
            "namespace": pod.metadata.namespace,
            "phase": phase,
            "restarts": restarts,
            "node": pod.spec.node_name if pod.spec else None,
            "age": str(pod.metadata.creation_timestamp),
        })
    return results


def get_pod(cluster: str, namespace: str, name: str) -> dict[str, Any]:
    """Get a single pod by name."""
    pod = _core(cluster).read_namespaced_pod(name, namespace)
    return _serialize(pod)


def get_pod_logs(cluster: str, namespace: str, name: str,
                 container: str | None = None, tail_lines: int = 100,
                 previous: bool = False) -> str:
    """Get logs for a pod."""
    kwargs: dict[str, Any] = {"tail_lines": tail_lines}
    if container:
        kwargs["container"] = container
    if previous:
        kwargs["previous"] = True
    return _core(cluster).read_namespaced_pod_log(name, namespace, **kwargs)


def delete_pod(cluster: str, namespace: str, name: str) -> dict[str, str]:
    """Delete a pod."""
    _core(cluster).delete_namespaced_pod(name, namespace)
    return {"status": "deleted", "pod": f"{namespace}/{name}", "cluster": cluster}


def restart_pod(cluster: str, namespace: str, name: str) -> dict[str, str]:
    """Restart a pod by deleting it (relies on controller to recreate)."""
    return delete_pod(cluster, namespace, name)


# ── Deployment Operations ────────────────────────────────────────────────────


def list_deployments(cluster: str, namespace: str = "") -> list[dict[str, Any]]:
    """List deployments."""
    v1 = _apps(cluster)
    if namespace:
        dep_list = v1.list_namespaced_deployment(namespace)
    else:
        dep_list = v1.list_deployment_for_all_namespaces()

    results = []
    for dep in dep_list.items:
        results.append({
            "name": dep.metadata.name,
            "namespace": dep.metadata.namespace,
            "replicas": dep.spec.replicas,
            "ready_replicas": dep.status.ready_replicas or 0,
            "available_replicas": dep.status.available_replicas or 0,
            "updated_replicas": dep.status.updated_replicas or 0,
        })
    return results


def get_deployment(cluster: str, namespace: str, name: str) -> dict[str, Any]:
    """Get a single deployment."""
    dep = _apps(cluster).read_namespaced_deployment(name, namespace)
    return _serialize(dep)


def scale_deployment(cluster: str, namespace: str, name: str, replicas: int) -> dict[str, Any]:
    """Scale a deployment to the given replica count."""
    body = {"spec": {"replicas": replicas}}
    _apps(cluster).patch_namespaced_deployment_scale(name, namespace, body)
    return {"status": "scaled", "deployment": f"{namespace}/{name}", "replicas": replicas, "cluster": cluster}


def restart_deployment(cluster: str, namespace: str, name: str) -> dict[str, str]:
    """Trigger a rolling restart of a deployment by patching template annotation."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": now
                    }
                }
            }
        }
    }
    _apps(cluster).patch_namespaced_deployment(name, namespace, body)
    return {"status": "restarted", "deployment": f"{namespace}/{name}", "cluster": cluster}


# ── Node Operations ──────────────────────────────────────────────────────────


def list_nodes(cluster: str) -> list[dict[str, Any]]:
    """List cluster nodes with status."""
    nodes = _core(cluster).list_node()
    results = []
    for node in nodes.items:
        conditions = {c.type: c.status for c in (node.status.conditions or [])}
        results.append({
            "name": node.metadata.name,
            "ready": conditions.get("Ready", "Unknown"),
            "roles": ",".join(
                k.replace("node-role.kubernetes.io/", "")
                for k in (node.metadata.labels or {})
                if k.startswith("node-role.kubernetes.io/")
            ) or "worker",
            "os_image": node.status.node_info.os_image if node.status.node_info else "",
            "kubelet_version": node.status.node_info.kubelet_version if node.status.node_info else "",
        })
    return results


def cordon_node(cluster: str, name: str) -> dict[str, str]:
    """Cordon a node (mark unschedulable)."""
    body = {"spec": {"unschedulable": True}}
    _core(cluster).patch_node(name, body)
    return {"status": "cordoned", "node": name, "cluster": cluster}


def uncordon_node(cluster: str, name: str) -> dict[str, str]:
    """Uncordon a node (mark schedulable)."""
    body = {"spec": {"unschedulable": False}}
    _core(cluster).patch_node(name, body)
    return {"status": "uncordoned", "node": name, "cluster": cluster}


# ── Event Operations ─────────────────────────────────────────────────────────


def list_events(cluster: str, namespace: str = "",
                event_type: str = "") -> list[dict[str, Any]]:
    """List events, optionally filtered by namespace and type."""
    v1 = _core(cluster)
    kwargs: dict[str, Any] = {}
    if event_type:
        kwargs["field_selector"] = f"type={event_type}"

    if namespace:
        events = v1.list_namespaced_event(namespace, **kwargs)
    else:
        events = v1.list_event_for_all_namespaces(**kwargs)

    results = []
    for ev in events.items:
        results.append({
            "type": ev.type,
            "reason": ev.reason,
            "message": ev.message,
            "namespace": ev.metadata.namespace,
            "involved_object": f"{ev.involved_object.kind}/{ev.involved_object.name}" if ev.involved_object else "",
            "count": ev.count,
            "last_timestamp": str(ev.last_timestamp),
        })
    return results


# ── Generic Resource Operations ──────────────────────────────────────────────


def get_resource(cluster: str, api_version: str, kind: str,
                 name: str, namespace: str | None = None) -> dict[str, Any]:
    """Get any Kubernetes resource by apiVersion/kind."""
    group, version = _parse_api_version(api_version)
    plural = _kind_to_plural(kind)
    custom = _custom(cluster)

    try:
        if namespace:
            obj = custom.get_namespaced_custom_object(group, version, namespace, plural, name)
        else:
            obj = custom.get_cluster_custom_object(group, version, plural, name)
        return obj
    except ApiException as e:
        return {"error": str(e)}


def list_resources(cluster: str, api_version: str, kind: str,
                   namespace: str | None = None) -> list[dict[str, Any]]:
    """List any Kubernetes resources by apiVersion/kind."""
    group, version = _parse_api_version(api_version)
    plural = _kind_to_plural(kind)
    custom = _custom(cluster)

    try:
        if namespace:
            obj = custom.list_namespaced_custom_object(group, version, namespace, plural)
        else:
            obj = custom.list_cluster_custom_object(group, version, plural)
        return obj.get("items", [])
    except ApiException as e:
        return [{"error": str(e)}]


def create_or_update_resource(cluster: str, manifest: dict[str, Any]) -> dict[str, Any]:
    """Create or update a resource from a manifest dict."""
    from kubernetes import utils as k8s_utils
    try:
        k8s_utils.create_from_dict(_api(cluster), manifest)
        return {"status": "created", "kind": manifest.get("kind"), "name": manifest.get("metadata", {}).get("name")}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def delete_resource(cluster: str, api_version: str, kind: str,
                    name: str, namespace: str | None = None) -> dict[str, Any]:
    """Delete any Kubernetes resource."""
    group, version = _parse_api_version(api_version)
    plural = _kind_to_plural(kind)
    custom = _custom(cluster)

    try:
        if namespace:
            custom.delete_namespaced_custom_object(group, version, namespace, plural, name)
        else:
            custom.delete_cluster_custom_object(group, version, plural, name)
        return {"status": "deleted", "kind": kind, "name": name}
    except ApiException as e:
        return {"error": str(e)}


# ── Service Operations ───────────────────────────────────────────────────────


def list_services(cluster: str, namespace: str = "") -> list[dict[str, Any]]:
    """List services."""
    v1 = _core(cluster)
    if namespace:
        svc_list = v1.list_namespaced_service(namespace)
    else:
        svc_list = v1.list_service_for_all_namespaces()

    results = []
    for svc in svc_list.items:
        results.append({
            "name": svc.metadata.name,
            "namespace": svc.metadata.namespace,
            "type": svc.spec.type,
            "cluster_ip": svc.spec.cluster_ip,
            "ports": [
                {"port": p.port, "target_port": str(p.target_port), "protocol": p.protocol}
                for p in (svc.spec.ports or [])
            ],
        })
    return results


# ── ConfigMap / Secret Operations ────────────────────────────────────────────


def list_configmaps(cluster: str, namespace: str) -> list[dict[str, str]]:
    """List configmap names in a namespace."""
    cms = _core(cluster).list_namespaced_config_map(namespace)
    return [{"name": cm.metadata.name, "namespace": cm.metadata.namespace} for cm in cms.items]


def get_configmap(cluster: str, namespace: str, name: str) -> dict[str, Any]:
    """Get a configmap."""
    cm = _core(cluster).read_namespaced_config_map(name, namespace)
    return {"name": cm.metadata.name, "data": cm.data or {}}


def list_secrets(cluster: str, namespace: str) -> list[dict[str, str]]:
    """List secrets (names and types only – no data)."""
    secrets = _core(cluster).list_namespaced_secret(namespace)
    return [{"name": s.metadata.name, "type": s.type} for s in secrets.items]


# ── Private helpers ──────────────────────────────────────────────────────────


def _parse_api_version(api_version: str) -> tuple[str, str]:
    """Split apiVersion into (group, version). Core API → ('', 'v1')."""
    if "/" in api_version:
        parts = api_version.split("/", 1)
        return parts[0], parts[1]
    return "", api_version


def _kind_to_plural(kind: str) -> str:
    """Naive Kind→plural conversion."""
    kind_lower = kind.lower()
    if kind_lower.endswith("s"):
        return kind_lower + "es"
    if kind_lower.endswith("y"):
        return kind_lower[:-1] + "ies"
    return kind_lower + "s"
