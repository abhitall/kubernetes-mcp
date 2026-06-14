"""
Kubernetes MCP Server – Core Kubernetes Operations

Provides comprehensive CRUD and inspection operations on Kubernetes resources.
Includes enriched metadata (labels, annotations, images, resource requests/limits),
label/field selector filtering, resource metrics, exec capabilities, and
workload-specific tools for StatefulSets, DaemonSets, Jobs, Ingresses, PVCs, HPAs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream as k8s_stream

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


def _autoscaling(cluster: str) -> k8s_client.AutoscalingV2Api:
    return k8s_client.AutoscalingV2Api(_api(cluster))


def _networking(cluster: str) -> k8s_client.NetworkingV1Api:
    return k8s_client.NetworkingV1Api(_api(cluster))


def _custom(cluster: str) -> k8s_client.CustomObjectsApi:
    return k8s_client.CustomObjectsApi(_api(cluster))


def _serialize(obj: Any) -> dict | list | str:
    """Best-effort serialize a K8s API object to dict."""
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return str(obj)


def _age(ts) -> str:
    """Human-readable age from a timestamp."""
    if not ts:
        return "unknown"
    if isinstance(ts, str):
        return ts
    now = datetime.now(timezone.utc)
    delta = now - ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else now - ts
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m"
    elif seconds < 86400:
        return f"{seconds // 3600}h"
    else:
        return f"{seconds // 86400}d"


def _safe_labels(obj) -> dict[str, str]:
    """Extract labels dict safely."""
    if obj and obj.metadata and obj.metadata.labels:
        return dict(obj.metadata.labels)
    return {}


def _safe_annotations(obj) -> dict[str, str]:
    """Extract annotations dict safely, filtering out large internal ones."""
    if obj and obj.metadata and obj.metadata.annotations:
        # Filter out kubectl last-applied-configuration (very large, noisy)
        return {
            k: v for k, v in obj.metadata.annotations.items()
            if k != "kubectl.kubernetes.io/last-applied-configuration"
        }
    return {}


def _container_images(spec) -> list[str]:
    """Extract container image list from a pod spec."""
    if not spec or not spec.containers:
        return []
    return [c.image for c in spec.containers if c.image]


def _container_resources(spec) -> list[dict[str, Any]]:
    """Extract resource requests/limits from containers."""
    if not spec or not spec.containers:
        return []
    result = []
    for c in spec.containers:
        entry = {"name": c.name}
        if c.resources:
            if c.resources.requests:
                entry["requests"] = dict(c.resources.requests)
            if c.resources.limits:
                entry["limits"] = dict(c.resources.limits)
        result.append(entry)
    return result


# ── Namespace Operations ─────────────────────────────────────────────────────


def list_namespaces(cluster: str, label_selector: str = "",
                    field_selector: str = "") -> list[dict[str, Any]]:
    """List all namespaces in a cluster with labels, annotations, and status."""
    kwargs: dict[str, Any] = {}
    if label_selector:
        kwargs["label_selector"] = label_selector
    if field_selector:
        kwargs["field_selector"] = field_selector

    ns_list = _core(cluster).list_namespace(**kwargs)
    results = []
    for ns in ns_list.items:
        results.append({
            "name": ns.metadata.name,
            "status": ns.status.phase if ns.status else "Unknown",
            "labels": ns.metadata.labels or {},
            "annotations": _safe_annotations(ns),
            "age": _age(ns.metadata.creation_timestamp),
        })
    return results


# ── Pod Operations ───────────────────────────────────────────────────────────


def list_pods(cluster: str, namespace: str = "",
              label_selector: str = "", field_selector: str = "",
              limit: int = 0) -> list[dict[str, Any]]:
    """List pods with enriched metadata including labels, annotations, images, resources, and status.

    Supports filtering by label_selector (e.g. 'app=myapp,env=prod')
    and field_selector (e.g. 'status.phase=Running,spec.nodeName=node1').
    Set ``limit`` (>0) to cap the number of pods returned and keep the response
    token-efficient; prefer narrowing with selectors over fetching everything.
    """
    v1 = _core(cluster)
    kwargs: dict[str, Any] = {}
    if label_selector:
        kwargs["label_selector"] = label_selector
    if field_selector:
        kwargs["field_selector"] = field_selector
    if limit and limit > 0:
        kwargs["limit"] = limit

    if namespace:
        pod_list = v1.list_namespaced_pod(namespace, **kwargs)
    else:
        pod_list = v1.list_pod_for_all_namespaces(**kwargs)

    results = []
    for pod in pod_list.items:
        phase = pod.status.phase if pod.status else "Unknown"
        restarts = 0
        ready_containers = 0
        total_containers = len(pod.spec.containers) if pod.spec and pod.spec.containers else 0

        if pod.status and pod.status.container_statuses:
            restarts = sum(cs.restart_count for cs in pod.status.container_statuses)
            ready_containers = sum(1 for cs in pod.status.container_statuses if cs.ready)

        # Determine QoS class
        qos_class = pod.status.qos_class if pod.status and pod.status.qos_class else "Unknown"

        results.append({
            "name": pod.metadata.name,
            "namespace": pod.metadata.namespace,
            "phase": phase,
            "ready": f"{ready_containers}/{total_containers}",
            "restarts": restarts,
            "node": pod.spec.node_name if pod.spec else None,
            "pod_ip": pod.status.pod_ip if pod.status else None,
            "age": _age(pod.metadata.creation_timestamp),
            "labels": pod.metadata.labels or {},
            "annotations": _safe_annotations(pod),
            "images": _container_images(pod.spec),
            "resources": _container_resources(pod.spec),
            "qos_class": qos_class,
            "service_account": pod.spec.service_account_name if pod.spec else None,
        })
    return results


def get_pod(cluster: str, namespace: str, name: str,
            response_format: str = "detailed") -> dict[str, Any]:
    """Get a single pod by name.

    ``response_format="detailed"`` (default) returns the full pod object;
    ``"concise"`` returns a small high-signal summary (~1/10th the tokens). For
    debugging prefer ``get_pod_context``, which also folds in events and logs.
    """
    pod = _core(cluster).read_namespaced_pod(name, namespace)
    if response_format == "concise":
        total = len(pod.spec.containers) if pod.spec and pod.spec.containers else 0
        restarts = ready = 0
        if pod.status and pod.status.container_statuses:
            restarts = sum(cs.restart_count or 0 for cs in pod.status.container_statuses)
            ready = sum(1 for cs in pod.status.container_statuses if cs.ready)
        return {
            "name": pod.metadata.name,
            "namespace": pod.metadata.namespace,
            "phase": pod.status.phase if pod.status else "Unknown",
            "ready": f"{ready}/{total}",
            "restarts": restarts,
            "node": pod.spec.node_name if pod.spec else None,
            "pod_ip": pod.status.pod_ip if pod.status else None,
            "images": _container_images(pod.spec),
            "qos_class": (pod.status.qos_class if pod.status else None) or "Unknown",
            "age": _age(pod.metadata.creation_timestamp),
        }
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


def exec_pod(cluster: str, namespace: str, name: str,
             command: list[str], container: str | None = None) -> dict[str, Any]:
    """Execute a command in a running pod and return stdout/stderr."""
    v1 = _core(cluster)
    kwargs: dict[str, Any] = {
        "command": command,
        "stderr": True,
        "stdout": True,
        "stdin": False,
        "tty": False,
    }
    if container:
        kwargs["container"] = container

    try:
        result = k8s_stream(
            v1.connect_get_namespaced_pod_exec,
            name, namespace, **kwargs
        )
        return {
            "status": "success",
            "pod": f"{namespace}/{name}",
            "command": command,
            "output": result,
        }
    except ApiException as e:
        return {"status": "error", "error": str(e)}


def top_pods(cluster: str, namespace: str = "") -> list[dict[str, Any]]:
    """Get resource usage (CPU/memory) for pods from the metrics API."""
    custom = _custom(cluster)
    try:
        if namespace:
            metrics = custom.list_namespaced_custom_object(
                "metrics.k8s.io", "v1beta1", namespace, "pods"
            )
        else:
            metrics = custom.list_cluster_custom_object(
                "metrics.k8s.io", "v1beta1", "pods"
            )

        results = []
        for pod in metrics.get("items", []):
            containers = []
            for c in pod.get("containers", []):
                containers.append({
                    "name": c.get("name"),
                    "cpu": c.get("usage", {}).get("cpu", "0"),
                    "memory": c.get("usage", {}).get("memory", "0"),
                })
            results.append({
                "name": pod["metadata"]["name"],
                "namespace": pod["metadata"]["namespace"],
                "containers": containers,
                "timestamp": pod.get("timestamp", ""),
            })
        return results
    except ApiException as e:
        if e.status == 404:
            return [{"error": "Metrics API not available. Install metrics-server."}]
        return [{"error": str(e)}]


# ── Deployment Operations ────────────────────────────────────────────────────


def list_deployments(cluster: str, namespace: str = "",
                     label_selector: str = "") -> list[dict[str, Any]]:
    """List deployments with enriched metadata including labels, annotations,
    strategy, images, conditions, and age."""
    v1 = _apps(cluster)
    kwargs: dict[str, Any] = {}
    if label_selector:
        kwargs["label_selector"] = label_selector

    if namespace:
        dep_list = v1.list_namespaced_deployment(namespace, **kwargs)
    else:
        dep_list = v1.list_deployment_for_all_namespaces(**kwargs)

    results = []
    for dep in dep_list.items:
        # Extract conditions
        conditions = []
        if dep.status and dep.status.conditions:
            for c in dep.status.conditions:
                conditions.append({
                    "type": c.type,
                    "status": c.status,
                    "reason": c.reason,
                    "message": c.message,
                })

        results.append({
            "name": dep.metadata.name,
            "namespace": dep.metadata.namespace,
            "replicas": dep.spec.replicas,
            "ready_replicas": dep.status.ready_replicas or 0,
            "available_replicas": dep.status.available_replicas or 0,
            "updated_replicas": dep.status.updated_replicas or 0,
            "age": _age(dep.metadata.creation_timestamp),
            "labels": dep.metadata.labels or {},
            "annotations": _safe_annotations(dep),
            "strategy": dep.spec.strategy.type if dep.spec.strategy else "RollingUpdate",
            "images": _container_images(dep.spec.template.spec) if dep.spec.template and dep.spec.template.spec else [],
            "conditions": conditions,
            "selector": dep.spec.selector.match_labels if dep.spec.selector else {},
        })
    return results


def get_deployment(cluster: str, namespace: str, name: str) -> dict[str, Any]:
    """Get a single deployment with full detail."""
    dep = _apps(cluster).read_namespaced_deployment(name, namespace)
    return _serialize(dep)


def scale_deployment(cluster: str, namespace: str, name: str, replicas: int) -> dict[str, Any]:
    """Scale a deployment to the given replica count."""
    body = {"spec": {"replicas": replicas}}
    _apps(cluster).patch_namespaced_deployment_scale(name, namespace, body)
    return {"status": "scaled", "deployment": f"{namespace}/{name}", "replicas": replicas, "cluster": cluster}


def restart_deployment(cluster: str, namespace: str, name: str) -> dict[str, str]:
    """Trigger a rolling restart of a deployment by patching template annotation."""
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc).isoformat()
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


def get_rollout_status(cluster: str, namespace: str, name: str) -> dict[str, Any]:
    """Get the rollout status of a deployment (similar to kubectl rollout status)."""
    dep = _apps(cluster).read_namespaced_deployment(name, namespace)

    desired = dep.spec.replicas or 1
    updated = dep.status.updated_replicas or 0
    available = dep.status.available_replicas or 0
    ready = dep.status.ready_replicas or 0
    observed_gen = dep.status.observed_generation or 0
    generation = dep.metadata.generation or 0

    # Determine rollout status
    if observed_gen < generation:
        status = "Progressing"
        message = "Waiting for deployment spec update to be observed..."
    elif updated < desired:
        status = "Progressing"
        message = f"Waiting for rollout: {updated} of {desired} updated replicas are available..."
    elif available < desired:
        status = "Progressing"
        message = f"Waiting for rollout: {available} of {desired} replicas available ({desired - available} unavailable)..."
    elif updated == desired and available == desired:
        status = "Complete"
        message = f"Deployment successfully rolled out. {desired}/{desired} replicas available."
    else:
        status = "Unknown"
        message = "Unable to determine rollout status."

    # Conditions
    conditions = []
    if dep.status and dep.status.conditions:
        for c in dep.status.conditions:
            conditions.append({
                "type": c.type,
                "status": c.status,
                "reason": c.reason,
                "message": c.message,
                "last_update": str(c.last_update_time),
            })

    return {
        "deployment": f"{namespace}/{name}",
        "status": status,
        "message": message,
        "desired_replicas": desired,
        "updated_replicas": updated,
        "ready_replicas": ready,
        "available_replicas": available,
        "generation": generation,
        "observed_generation": observed_gen,
        "conditions": conditions,
    }


def rollback_deployment(cluster: str, namespace: str, name: str,
                        revision: int = 0) -> dict[str, Any]:
    """Rollback a deployment. If revision=0, rolls back to the previous revision.
    Otherwise rolls back to the specified revision."""
    apps = _apps(cluster)

    # Get the ReplicaSets for this deployment to find revisions
    dep = apps.read_namespaced_deployment(name, namespace)
    selector = dep.spec.selector
    if not selector or not selector.match_labels:
        return {"error": "Deployment has no selector labels."}

    label_str = ",".join(f"{k}={v}" for k, v in selector.match_labels.items())
    rs_list = apps.list_namespaced_replica_set(namespace, label_selector=label_str)

    # Find ReplicaSets owned by this deployment
    owned_rs = []
    for rs in rs_list.items:
        if rs.metadata.owner_references:
            for ref in rs.metadata.owner_references:
                if ref.kind == "Deployment" and ref.name == name:
                    rev_str = (rs.metadata.annotations or {}).get(
                        "deployment.kubernetes.io/revision", "0"
                    )
                    owned_rs.append((int(rev_str), rs))
                    break

    if not owned_rs:
        return {"error": "No ReplicaSets found for this deployment."}

    owned_rs.sort(key=lambda x: x[0])

    if revision == 0:
        # Rollback to previous (second-to-last)
        if len(owned_rs) < 2:
            return {"error": "No previous revision available to rollback to."}
        target_rs = owned_rs[-2][1]
        target_revision = owned_rs[-2][0]
    else:
        # Find the specific revision
        target_rs = None
        target_revision = revision
        for rev, rs in owned_rs:
            if rev == revision:
                target_rs = rs
                break
        if not target_rs:
            available = [r[0] for r in owned_rs]
            return {"error": f"Revision {revision} not found. Available: {available}"}

    # Patch the deployment with the target RS template
    patch = {
        "spec": {
            "template": _serialize(target_rs.spec.template)
        }
    }
    apps.patch_namespaced_deployment(name, namespace, patch)
    return {
        "status": "rolled_back",
        "deployment": f"{namespace}/{name}",
        "target_revision": target_revision,
        "cluster": cluster,
    }


# ── Node Operations ──────────────────────────────────────────────────────────


def list_nodes(cluster: str, label_selector: str = "") -> list[dict[str, Any]]:
    """List cluster nodes with enriched status including capacity, allocatable,
    taints, conditions, and internal IPs."""
    kwargs: dict[str, Any] = {}
    if label_selector:
        kwargs["label_selector"] = label_selector

    nodes = _core(cluster).list_node(**kwargs)
    results = []
    for node in nodes.items:
        conditions = {c.type: c.status for c in (node.status.conditions or [])}

        # Extract capacity and allocatable
        capacity = {}
        allocatable = {}
        if node.status.capacity:
            capacity = {
                "cpu": node.status.capacity.get("cpu", ""),
                "memory": node.status.capacity.get("memory", ""),
                "pods": node.status.capacity.get("pods", ""),
            }
        if node.status.allocatable:
            allocatable = {
                "cpu": node.status.allocatable.get("cpu", ""),
                "memory": node.status.allocatable.get("memory", ""),
                "pods": node.status.allocatable.get("pods", ""),
            }

        # Extract taints
        taints = []
        if node.spec and node.spec.taints:
            for t in node.spec.taints:
                taints.append({
                    "key": t.key,
                    "value": t.value,
                    "effect": t.effect,
                })

        # Extract internal/external IPs
        internal_ip = ""
        external_ip = ""
        if node.status.addresses:
            for addr in node.status.addresses:
                if addr.type == "InternalIP":
                    internal_ip = addr.address
                elif addr.type == "ExternalIP":
                    external_ip = addr.address

        # All conditions detail
        conditions_detail = []
        for c in (node.status.conditions or []):
            conditions_detail.append({
                "type": c.type,
                "status": c.status,
                "reason": c.reason,
                "message": c.message,
            })

        results.append({
            "name": node.metadata.name,
            "ready": conditions.get("Ready", "Unknown"),
            "roles": ",".join(
                k.replace("node-role.kubernetes.io/", "")
                for k in (node.metadata.labels or {})
                if k.startswith("node-role.kubernetes.io/")
            ) or "worker",
            "age": _age(node.metadata.creation_timestamp),
            "os_image": node.status.node_info.os_image if node.status.node_info else "",
            "kubelet_version": node.status.node_info.kubelet_version if node.status.node_info else "",
            "container_runtime": node.status.node_info.container_runtime_version if node.status.node_info else "",
            "internal_ip": internal_ip,
            "external_ip": external_ip,
            "capacity": capacity,
            "allocatable": allocatable,
            "taints": taints,
            "conditions": conditions_detail,
            "labels": node.metadata.labels or {},
            "annotations": _safe_annotations(node),
            "unschedulable": node.spec.unschedulable if node.spec else False,
        })
    return results


def top_nodes(cluster: str) -> list[dict[str, Any]]:
    """Get resource usage (CPU/memory) for nodes from the metrics API."""
    custom = _custom(cluster)
    try:
        metrics = custom.list_cluster_custom_object(
            "metrics.k8s.io", "v1beta1", "nodes"
        )
        results = []
        for node in metrics.get("items", []):
            usage = node.get("usage", {})
            results.append({
                "name": node["metadata"]["name"],
                "cpu": usage.get("cpu", "0"),
                "memory": usage.get("memory", "0"),
                "timestamp": node.get("timestamp", ""),
            })
        return results
    except ApiException as e:
        if e.status == 404:
            return [{"error": "Metrics API not available. Install metrics-server."}]
        return [{"error": str(e)}]


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
                event_type: str = "", field_selector: str = "",
                limit: int = 100) -> list[dict[str, Any]]:
    """List events with enriched metadata. Supports filtering by type and field_selector.

    field_selector examples: 'involvedObject.name=mypod', 'type=Warning'
    """
    v1 = _core(cluster)
    kwargs: dict[str, Any] = {}

    # Build field selector combining type filter and custom selector
    selectors = []
    if event_type:
        selectors.append(f"type={event_type}")
    if field_selector:
        selectors.append(field_selector)
    if selectors:
        kwargs["field_selector"] = ",".join(selectors)

    if limit:
        kwargs["limit"] = limit

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
            "involved_object_namespace": ev.involved_object.namespace if ev.involved_object else None,
            "count": ev.count,
            "first_timestamp": str(ev.first_timestamp) if ev.first_timestamp else None,
            "last_timestamp": str(ev.last_timestamp) if ev.last_timestamp else None,
            "source": f"{ev.source.component}/{ev.source.host}" if ev.source else "",
            "age": _age(ev.last_timestamp or ev.metadata.creation_timestamp),
        })
    return results


# ── Service Operations ───────────────────────────────────────────────────────


def list_services(cluster: str, namespace: str = "",
                  label_selector: str = "") -> list[dict[str, Any]]:
    """List services with enriched metadata including selectors, labels, annotations, and endpoints."""
    v1 = _core(cluster)
    kwargs: dict[str, Any] = {}
    if label_selector:
        kwargs["label_selector"] = label_selector

    if namespace:
        svc_list = v1.list_namespaced_service(namespace, **kwargs)
    else:
        svc_list = v1.list_service_for_all_namespaces(**kwargs)

    results = []
    for svc in svc_list.items:
        external_ips = []
        if svc.spec.external_i_ps:
            external_ips = svc.spec.external_i_ps
        elif svc.status and svc.status.load_balancer and svc.status.load_balancer.ingress:
            external_ips = [
                ing.ip or ing.hostname
                for ing in svc.status.load_balancer.ingress
                if ing.ip or ing.hostname
            ]

        results.append({
            "name": svc.metadata.name,
            "namespace": svc.metadata.namespace,
            "type": svc.spec.type,
            "cluster_ip": svc.spec.cluster_ip,
            "external_ips": external_ips,
            "ports": [
                {"port": p.port, "target_port": str(p.target_port), "protocol": p.protocol, "name": p.name}
                for p in (svc.spec.ports or [])
            ],
            "selector": dict(svc.spec.selector) if svc.spec.selector else {},
            "age": _age(svc.metadata.creation_timestamp),
            "labels": svc.metadata.labels or {},
            "annotations": _safe_annotations(svc),
        })
    return results


# ── ConfigMap / Secret Operations ────────────────────────────────────────────


def list_configmaps(cluster: str, namespace: str,
                    label_selector: str = "") -> list[dict[str, Any]]:
    """List configmaps with labels and age."""
    kwargs: dict[str, Any] = {}
    if label_selector:
        kwargs["label_selector"] = label_selector

    cms = _core(cluster).list_namespaced_config_map(namespace, **kwargs)
    return [{
        "name": cm.metadata.name,
        "namespace": cm.metadata.namespace,
        "data_keys": list((cm.data or {}).keys()),
        "labels": cm.metadata.labels or {},
        "annotations": _safe_annotations(cm),
        "age": _age(cm.metadata.creation_timestamp),
    } for cm in cms.items]


def get_configmap(cluster: str, namespace: str, name: str) -> dict[str, Any]:
    """Get a configmap with full data."""
    cm = _core(cluster).read_namespaced_config_map(name, namespace)
    return {
        "name": cm.metadata.name,
        "namespace": cm.metadata.namespace,
        "data": cm.data or {},
        "labels": cm.metadata.labels or {},
        "annotations": _safe_annotations(cm),
        "age": _age(cm.metadata.creation_timestamp),
    }


def list_secrets(cluster: str, namespace: str,
                 label_selector: str = "") -> list[dict[str, Any]]:
    """List secrets (names, types, labels – no data exposed)."""
    kwargs: dict[str, Any] = {}
    if label_selector:
        kwargs["label_selector"] = label_selector

    secrets = _core(cluster).list_namespaced_secret(namespace, **kwargs)
    return [{
        "name": s.metadata.name,
        "namespace": s.metadata.namespace,
        "type": s.type,
        "data_keys": list((s.data or {}).keys()),
        "labels": s.metadata.labels or {},
        "annotations": _safe_annotations(s),
        "age": _age(s.metadata.creation_timestamp),
    } for s in secrets.items]


# ── Generic Resource Operations ──────────────────────────────────────────────


def _core_api_request(cluster: str, version: str, plural: str, *,
                      name: str | None = None, namespace: str | None = None,
                      query: dict[str, Any] | None = None,
                      method: str = "GET") -> Any:
    """Raw request against the core API group (``/api/{version}/...``).

    ``CustomObjectsApi`` only serves grouped APIs (``/apis/{group}/...``), so
    core kinds (Pod, Service, ConfigMap, Node, …) must be reached via the core
    path directly or the request 404s. Auth headers are taken from the
    ApiClient's default headers configured by the connector.
    """
    import json as _json

    api_client = _api(cluster)
    if namespace:
        path = f"/api/{version}/namespaces/{namespace}/{plural}"
    else:
        path = f"/api/{version}/{plural}"
    if name:
        path += f"/{name}"

    resp = api_client.call_api(
        path,
        method,
        query_params=list((query or {}).items()),
        header_params={"Accept": "application/json"},
        auth_settings=[],
        _preload_content=False,
        _return_http_data_only=True,
    )
    return _json.loads(resp.data)


def get_resource(cluster: str, api_version: str, kind: str,
                 name: str, namespace: str | None = None) -> dict[str, Any]:
    """Get any Kubernetes resource by apiVersion/kind (core or CRD)."""
    group, version = _parse_api_version(api_version)
    plural, _ = _resolve_resource(cluster, api_version, kind)

    try:
        if group == "":
            # Core API group — CustomObjectsApi cannot reach /api/v1/...
            return _core_api_request(cluster, version, plural, name=name, namespace=namespace)
        custom = _custom(cluster)
        if namespace:
            obj = custom.get_namespaced_custom_object(group, version, namespace, plural, name)
        else:
            obj = custom.get_cluster_custom_object(group, version, plural, name)
        return obj
    except ApiException as e:
        return {"error": str(e)}


def list_resources(cluster: str, api_version: str, kind: str,
                   namespace: str | None = None,
                   label_selector: str = "",
                   field_selector: str = "",
                   limit: int = 0) -> list[dict[str, Any]]:
    """List any Kubernetes resources by apiVersion/kind with optional label/field selectors.

    Set ``limit`` (>0) to cap the number of items returned and keep the response
    token-efficient on large or unknown CRD collections."""
    group, version = _parse_api_version(api_version)
    plural, _ = _resolve_resource(cluster, api_version, kind)
    kwargs: dict[str, Any] = {}
    if label_selector:
        kwargs["label_selector"] = label_selector
    if field_selector:
        kwargs["field_selector"] = field_selector
    if limit and limit > 0:
        kwargs["limit"] = limit

    try:
        if group == "":
            # Core API group — route through /api/{version}/... directly.
            query: dict[str, Any] = {}
            if label_selector:
                query["labelSelector"] = label_selector
            if field_selector:
                query["fieldSelector"] = field_selector
            if limit and limit > 0:
                query["limit"] = limit
            obj = _core_api_request(cluster, version, plural, namespace=namespace, query=query)
            return obj.get("items", [])
        custom = _custom(cluster)
        if namespace:
            obj = custom.list_namespaced_custom_object(group, version, namespace, plural, **kwargs)
        else:
            obj = custom.list_cluster_custom_object(group, version, plural, **kwargs)
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
    """Delete any Kubernetes resource (core or CRD)."""
    group, version = _parse_api_version(api_version)
    plural, _ = _resolve_resource(cluster, api_version, kind)

    try:
        if group == "":
            # Core API group — CustomObjectsApi cannot reach /api/v1/...
            _core_api_request(cluster, version, plural, name=name,
                              namespace=namespace, method="DELETE")
            return {"status": "deleted", "kind": kind, "name": name}
        custom = _custom(cluster)
        if namespace:
            custom.delete_namespaced_custom_object(group, version, namespace, plural, name)
        else:
            custom.delete_cluster_custom_object(group, version, plural, name)
        return {"status": "deleted", "kind": kind, "name": name}
    except ApiException as e:
        return {"error": str(e)}


# ── Describe Resource (Rich Combined View) ───────────────────────────────────


def describe_resource(cluster: str, api_version: str, kind: str,
                      name: str, namespace: str | None = None) -> dict[str, Any]:
    """Get a rich description of any resource, combining its spec/status with
    related events (similar to 'kubectl describe')."""
    # Get the resource
    resource = get_resource(cluster, api_version, kind, name, namespace)
    if "error" in resource:
        return resource

    # Get related events
    v1 = _core(cluster)
    events_data = []
    try:
        if namespace:
            events = v1.list_namespaced_event(
                namespace,
                field_selector=f"involvedObject.name={name},involvedObject.kind={kind}",
            )
        else:
            events = v1.list_event_for_all_namespaces(
                field_selector=f"involvedObject.name={name},involvedObject.kind={kind}",
            )
        for ev in events.items:
            events_data.append({
                "type": ev.type,
                "reason": ev.reason,
                "message": ev.message,
                "count": ev.count,
                "age": _age(ev.last_timestamp or ev.metadata.creation_timestamp),
                "source": f"{ev.source.component}" if ev.source else "",
            })
    except Exception:
        pass  # Events are supplementary

    return {
        "resource": resource,
        "events": events_data,
    }


# ── API Discovery & Custom Resources (CRD / CR) ──────────────────────────────

# Cache of (cluster, api_version) -> APIResourceList.resources. Discovery docs
# are effectively static for the life of a process, so caching avoids a request
# per generic-resource call.
_DISCOVERY_CACHE: dict[tuple[str, str], list[dict[str, Any]]] = {}


def _discovery_doc(cluster: str, api_version: str) -> list[dict[str, Any]]:
    """Return the ``resources`` list from the discovery doc for an apiVersion.

    Core group → ``/api/{version}``; grouped APIs/CRDs → ``/apis/{group}/{version}``.
    Returns ``[]`` (cached) if discovery is unavailable so callers can fall back.
    """
    import json as _json

    key = (cluster, api_version)
    if key in _DISCOVERY_CACHE:
        return _DISCOVERY_CACHE[key]

    group, version = _parse_api_version(api_version)
    path = f"/api/{version}" if group == "" else f"/apis/{group}/{version}"
    try:
        resp = _api(cluster).call_api(
            path, "GET",
            header_params={"Accept": "application/json"},
            auth_settings=[], _preload_content=False, _return_http_data_only=True,
        )
        resources = _json.loads(resp.data).get("resources", []) or []
    except Exception:  # noqa: BLE001 - discovery is best-effort
        resources = []
    _DISCOVERY_CACHE[key] = resources
    return resources


def _resolve_resource(cluster: str, api_version: str, kind: str) -> tuple[str, bool | None]:
    """Resolve ``(plural, namespaced)`` for a Kind via API discovery, falling
    back to the heuristic pluralizer when discovery is unavailable.

    Discovery is authoritative: it returns the real plural and scope, which the
    ``_kind_to_plural`` heuristic gets wrong for many CRDs (e.g. Kind ``Gateway``
    → heuristic ``gatewaies`` vs real ``gateways``)."""
    for r in _discovery_doc(cluster, api_version):
        # Skip subresources (e.g. "pods/status") which carry a "/" in the name.
        if r.get("kind") == kind and "/" not in r.get("name", ""):
            return r["name"], r.get("namespaced")
    return _kind_to_plural(kind), None


def _all_group_versions(cluster: str) -> list[str]:
    """All served group/versions (core + each group's preferred version)."""
    import json as _json

    gvs = ["v1"]  # core API group
    try:
        resp = _api(cluster).call_api(
            "/apis", "GET",
            header_params={"Accept": "application/json"},
            auth_settings=[], _preload_content=False, _return_http_data_only=True,
        )
        for g in _json.loads(resp.data).get("groups", []):
            pv = (g.get("preferredVersion") or {}).get("groupVersion")
            if pv:
                gvs.append(pv)
    except Exception:  # noqa: BLE001
        pass
    return gvs


def list_api_resources(cluster: str, api_version: str = "") -> list[dict[str, Any]]:
    """List served API resources (like ``kubectl api-resources``).

    With ``api_version`` (e.g. ``cert-manager.io/v1``), lists that group's kinds.
    Without it, enumerates every served group at its preferred version — the way
    to discover which custom (and built-in) kinds exist before querying them."""
    gvs = [api_version] if api_version else _all_group_versions(cluster)
    results: list[dict[str, Any]] = []
    for gv in gvs:
        for r in _discovery_doc(cluster, gv):
            if "/" in r.get("name", ""):  # skip subresources
                continue
            results.append({
                "kind": r.get("kind"),
                "name": r.get("name"),
                "api_version": gv,
                "namespaced": r.get("namespaced"),
                "short_names": r.get("shortNames", []) or [],
                "verbs": r.get("verbs", []) or [],
            })
    return results


def list_crds(cluster: str, label_selector: str = "") -> list[dict[str, Any]]:
    """List CustomResourceDefinitions with the info needed to query their CRs:
    group, kind, plural, scope, served/storage versions, and short names."""
    custom = _custom(cluster)
    kwargs: dict[str, Any] = {}
    if label_selector:
        kwargs["label_selector"] = label_selector
    try:
        obj = custom.list_cluster_custom_object(
            "apiextensions.k8s.io", "v1", "customresourcedefinitions", **kwargs
        )
    except ApiException as e:
        return [{"error": str(e)}]

    out: list[dict[str, Any]] = []
    for item in obj.get("items", []):
        spec = item.get("spec", {}) or {}
        names = spec.get("names", {}) or {}
        versions = spec.get("versions", []) or []
        served = [v.get("name") for v in versions if v.get("served")]
        storage = next((v.get("name") for v in versions if v.get("storage")), None)
        out.append({
            "name": item.get("metadata", {}).get("name"),
            "group": spec.get("group"),
            "kind": names.get("kind"),
            "plural": names.get("plural"),
            "short_names": names.get("shortNames", []) or [],
            "scope": spec.get("scope"),
            "served_versions": served,
            "storage_version": storage,
            "created": item.get("metadata", {}).get("creationTimestamp"),
        })
    return out


def _crd_for_kind(cluster: str, kind: str) -> dict[str, Any] | None:
    """Find a CRD entry by Kind (case-insensitive)."""
    kl = kind.lower()
    for crd in list_crds(cluster):
        if (crd.get("kind") or "").lower() == kl:
            return crd
    return None


def _api_version_for_crd(crd: dict[str, Any]) -> str:
    version = crd.get("storage_version") or (crd.get("served_versions") or [None])[0]
    return f"{crd['group']}/{version}"


def list_custom_resources(cluster: str, kind: str, namespace: str | None = None,
                          label_selector: str = "", limit: int = 0) -> list[dict[str, Any]]:
    """List Custom Resources by Kind alone — the CRD is looked up to resolve its
    group, served version, plural, and scope, so the caller need not know them.

    For namespaced kinds, omit ``namespace`` to list across all namespaces."""
    crd = _crd_for_kind(cluster, kind)
    if not crd:
        return [{"error": f"No CRD found for kind '{kind}'. Use list_crds to see available kinds."}]
    api_version = _api_version_for_crd(crd)
    ns = namespace if crd.get("scope") == "Namespaced" else None
    return list_resources(cluster, api_version, crd["kind"], ns, label_selector, "", limit)


def get_custom_resource(cluster: str, kind: str, name: str,
                        namespace: str | None = None) -> dict[str, Any]:
    """Get a single Custom Resource by Kind and name (CRD resolved automatically)."""
    crd = _crd_for_kind(cluster, kind)
    if not crd:
        return {"error": f"No CRD found for kind '{kind}'. Use list_crds to see available kinds."}
    api_version = _api_version_for_crd(crd)
    ns = namespace if crd.get("scope") == "Namespaced" else None
    return get_resource(cluster, api_version, crd["kind"], name, ns)


# ── StatefulSet Operations ───────────────────────────────────────────────────


def list_statefulsets(cluster: str, namespace: str = "",
                     label_selector: str = "") -> list[dict[str, Any]]:
    """List StatefulSets with enriched metadata."""
    v1 = _apps(cluster)
    kwargs: dict[str, Any] = {}
    if label_selector:
        kwargs["label_selector"] = label_selector

    if namespace:
        sts_list = v1.list_namespaced_stateful_set(namespace, **kwargs)
    else:
        sts_list = v1.list_stateful_set_for_all_namespaces(**kwargs)

    results = []
    for sts in sts_list.items:
        results.append({
            "name": sts.metadata.name,
            "namespace": sts.metadata.namespace,
            "replicas": sts.spec.replicas,
            "ready_replicas": sts.status.ready_replicas or 0,
            "current_replicas": sts.status.current_replicas or 0,
            "updated_replicas": sts.status.updated_replicas or 0,
            "age": _age(sts.metadata.creation_timestamp),
            "labels": sts.metadata.labels or {},
            "annotations": _safe_annotations(sts),
            "images": _container_images(sts.spec.template.spec) if sts.spec.template and sts.spec.template.spec else [],
            "service_name": sts.spec.service_name,
            "update_strategy": sts.spec.update_strategy.type if sts.spec.update_strategy else "RollingUpdate",
        })
    return results


def get_statefulset(cluster: str, namespace: str, name: str) -> dict[str, Any]:
    """Get a single StatefulSet with full detail."""
    sts = _apps(cluster).read_namespaced_stateful_set(name, namespace)
    return _serialize(sts)


# ── DaemonSet Operations ─────────────────────────────────────────────────────


def list_daemonsets(cluster: str, namespace: str = "",
                   label_selector: str = "") -> list[dict[str, Any]]:
    """List DaemonSets with enriched metadata."""
    v1 = _apps(cluster)
    kwargs: dict[str, Any] = {}
    if label_selector:
        kwargs["label_selector"] = label_selector

    if namespace:
        ds_list = v1.list_namespaced_daemon_set(namespace, **kwargs)
    else:
        ds_list = v1.list_daemon_set_for_all_namespaces(**kwargs)

    results = []
    for ds in ds_list.items:
        results.append({
            "name": ds.metadata.name,
            "namespace": ds.metadata.namespace,
            "desired_number_scheduled": ds.status.desired_number_scheduled or 0,
            "current_number_scheduled": ds.status.current_number_scheduled or 0,
            "number_ready": ds.status.number_ready or 0,
            "number_available": ds.status.number_available or 0,
            "number_misscheduled": ds.status.number_misscheduled or 0,
            "age": _age(ds.metadata.creation_timestamp),
            "labels": ds.metadata.labels or {},
            "annotations": _safe_annotations(ds),
            "images": _container_images(ds.spec.template.spec) if ds.spec.template and ds.spec.template.spec else [],
            "node_selector": ds.spec.template.spec.node_selector if ds.spec.template and ds.spec.template.spec else None,
            "update_strategy": ds.spec.update_strategy.type if ds.spec.update_strategy else "RollingUpdate",
        })
    return results


def get_daemonset(cluster: str, namespace: str, name: str) -> dict[str, Any]:
    """Get a single DaemonSet with full detail."""
    ds = _apps(cluster).read_namespaced_daemon_set(name, namespace)
    return _serialize(ds)


# ── Job / CronJob Operations ────────────────────────────────────────────────


def list_jobs(cluster: str, namespace: str = "",
              label_selector: str = "") -> list[dict[str, Any]]:
    """List Jobs with status information."""
    v1 = _batch(cluster)
    kwargs: dict[str, Any] = {}
    if label_selector:
        kwargs["label_selector"] = label_selector

    if namespace:
        job_list = v1.list_namespaced_job(namespace, **kwargs)
    else:
        job_list = v1.list_job_for_all_namespaces(**kwargs)

    results = []
    for job in job_list.items:
        # Determine completion status
        succeeded = job.status.succeeded or 0
        failed = job.status.failed or 0
        active = job.status.active or 0
        completions = job.spec.completions or 1

        if succeeded >= completions:
            status = "Complete"
        elif failed > 0 and active == 0:
            status = "Failed"
        elif active > 0:
            status = "Running"
        else:
            status = "Pending"

        results.append({
            "name": job.metadata.name,
            "namespace": job.metadata.namespace,
            "status": status,
            "completions": f"{succeeded}/{completions}",
            "active": active,
            "failed": failed,
            "age": _age(job.metadata.creation_timestamp),
            "duration": str(job.status.completion_time - job.status.start_time) if job.status.completion_time and job.status.start_time else None,
            "labels": job.metadata.labels or {},
            "images": _container_images(job.spec.template.spec) if job.spec.template and job.spec.template.spec else [],
        })
    return results


def list_cronjobs(cluster: str, namespace: str = "",
                  label_selector: str = "") -> list[dict[str, Any]]:
    """List CronJobs with schedule and status information."""
    v1 = _batch(cluster)
    kwargs: dict[str, Any] = {}
    if label_selector:
        kwargs["label_selector"] = label_selector

    if namespace:
        cj_list = v1.list_namespaced_cron_job(namespace, **kwargs)
    else:
        cj_list = v1.list_cron_job_for_all_namespaces(**kwargs)

    results = []
    for cj in cj_list.items:
        results.append({
            "name": cj.metadata.name,
            "namespace": cj.metadata.namespace,
            "schedule": cj.spec.schedule,
            "suspend": cj.spec.suspend or False,
            "active_jobs": len(cj.status.active or []),
            "last_schedule": str(cj.status.last_schedule_time) if cj.status.last_schedule_time else None,
            "last_successful": str(cj.status.last_successful_time) if cj.status and hasattr(cj.status, 'last_successful_time') and cj.status.last_successful_time else None,
            "age": _age(cj.metadata.creation_timestamp),
            "labels": cj.metadata.labels or {},
            "concurrency_policy": cj.spec.concurrency_policy or "Allow",
        })
    return results


# ── Ingress Operations ───────────────────────────────────────────────────────


def list_ingresses(cluster: str, namespace: str = "",
                   label_selector: str = "") -> list[dict[str, Any]]:
    """List Ingresses with rules and backend information."""
    v1 = _networking(cluster)
    kwargs: dict[str, Any] = {}
    if label_selector:
        kwargs["label_selector"] = label_selector

    if namespace:
        ing_list = v1.list_namespaced_ingress(namespace, **kwargs)
    else:
        ing_list = v1.list_ingress_for_all_namespaces(**kwargs)

    results = []
    for ing in ing_list.items:
        # Extract rules
        rules = []
        if ing.spec.rules:
            for rule in ing.spec.rules:
                paths = []
                if rule.http and rule.http.paths:
                    for path in rule.http.paths:
                        backend = ""
                        if path.backend and path.backend.service:
                            port = ""
                            if path.backend.service.port:
                                port = str(path.backend.service.port.number or path.backend.service.port.name or "")
                            backend = f"{path.backend.service.name}:{port}"
                        paths.append({
                            "path": path.path or "/",
                            "path_type": path.path_type,
                            "backend": backend,
                        })
                rules.append({
                    "host": rule.host or "*",
                    "paths": paths,
                })

        # Extract TLS hosts
        tls_hosts = []
        if ing.spec.tls:
            for tls in ing.spec.tls:
                tls_hosts.extend(tls.hosts or [])

        # Extract load balancer IPs
        lb_ips = []
        if ing.status and ing.status.load_balancer and ing.status.load_balancer.ingress:
            lb_ips = [
                lb.ip or lb.hostname
                for lb in ing.status.load_balancer.ingress
                if lb.ip or lb.hostname
            ]

        results.append({
            "name": ing.metadata.name,
            "namespace": ing.metadata.namespace,
            "ingress_class": ing.spec.ingress_class_name,
            "rules": rules,
            "tls_hosts": tls_hosts,
            "load_balancer_ips": lb_ips,
            "age": _age(ing.metadata.creation_timestamp),
            "labels": ing.metadata.labels or {},
            "annotations": _safe_annotations(ing),
        })
    return results


# ── PVC Operations ───────────────────────────────────────────────────────────


def list_pvcs(cluster: str, namespace: str = "",
              label_selector: str = "") -> list[dict[str, Any]]:
    """List PersistentVolumeClaims with status and capacity information."""
    v1 = _core(cluster)
    kwargs: dict[str, Any] = {}
    if label_selector:
        kwargs["label_selector"] = label_selector

    if namespace:
        pvc_list = v1.list_namespaced_persistent_volume_claim(namespace, **kwargs)
    else:
        pvc_list = v1.list_persistent_volume_claim_for_all_namespaces(**kwargs)

    results = []
    for pvc in pvc_list.items:
        capacity = ""
        if pvc.status and pvc.status.capacity:
            capacity = pvc.status.capacity.get("storage", "")

        results.append({
            "name": pvc.metadata.name,
            "namespace": pvc.metadata.namespace,
            "status": pvc.status.phase if pvc.status else "Unknown",
            "volume": pvc.spec.volume_name,
            "capacity": capacity,
            "access_modes": pvc.spec.access_modes or [],
            "storage_class": pvc.spec.storage_class_name,
            "age": _age(pvc.metadata.creation_timestamp),
            "labels": pvc.metadata.labels or {},
            "annotations": _safe_annotations(pvc),
        })
    return results


# ── HPA Operations ───────────────────────────────────────────────────────────


def list_hpas(cluster: str, namespace: str = "",
              label_selector: str = "") -> list[dict[str, Any]]:
    """List HorizontalPodAutoscalers with current/target metrics."""
    v1 = _autoscaling(cluster)
    kwargs: dict[str, Any] = {}
    if label_selector:
        kwargs["label_selector"] = label_selector

    if namespace:
        hpa_list = v1.list_namespaced_horizontal_pod_autoscaler(namespace, **kwargs)
    else:
        hpa_list = v1.list_horizontal_pod_autoscaler_for_all_namespaces(**kwargs)

    results = []
    for hpa in hpa_list.items:
        # Extract metrics info
        metrics_info = []
        if hpa.spec.metrics:
            for m in hpa.spec.metrics:
                metric_entry = {"type": m.type}
                if m.type == "Resource" and m.resource:
                    metric_entry["name"] = m.resource.name
                    if m.resource.target:
                        metric_entry["target_type"] = m.resource.target.type
                        metric_entry["target_value"] = (
                            m.resource.target.average_utilization
                            or str(m.resource.target.average_value or m.resource.target.value or "")
                        )
                metrics_info.append(metric_entry)

        # Current metrics
        current_metrics = []
        if hpa.status and hpa.status.current_metrics:
            for cm in hpa.status.current_metrics:
                current = {"type": cm.type}
                if cm.type == "Resource" and cm.resource:
                    current["name"] = cm.resource.name
                    if cm.resource.current:
                        current["current_value"] = (
                            cm.resource.current.average_utilization
                            or str(cm.resource.current.average_value or "")
                        )
                current_metrics.append(current)

        results.append({
            "name": hpa.metadata.name,
            "namespace": hpa.metadata.namespace,
            "reference": f"{hpa.spec.scale_target_ref.kind}/{hpa.spec.scale_target_ref.name}" if hpa.spec.scale_target_ref else "",
            "min_replicas": hpa.spec.min_replicas,
            "max_replicas": hpa.spec.max_replicas,
            "current_replicas": hpa.status.current_replicas if hpa.status else 0,
            "desired_replicas": hpa.status.desired_replicas if hpa.status else 0,
            "metrics": metrics_info,
            "current_metrics": current_metrics,
            "age": _age(hpa.metadata.creation_timestamp),
            "labels": hpa.metadata.labels or {},
        })
    return results


# ── Private helpers ──────────────────────────────────────────────────────────


# Comprehensive Kind→plural mapping for common Kubernetes resource types
_KIND_PLURAL_MAP: dict[str, str] = {
    "pod": "pods",
    "service": "services",
    "endpoints": "endpoints",
    "node": "nodes",
    "namespace": "namespaces",
    "event": "events",
    "configmap": "configmaps",
    "secret": "secrets",
    "serviceaccount": "serviceaccounts",
    "persistentvolume": "persistentvolumes",
    "persistentvolumeclaim": "persistentvolumeclaims",
    "resourcequota": "resourcequotas",
    "limitrange": "limitranges",
    "replicationcontroller": "replicationcontrollers",
    "deployment": "deployments",
    "replicaset": "replicasets",
    "statefulset": "statefulsets",
    "daemonset": "daemonsets",
    "job": "jobs",
    "cronjob": "cronjobs",
    "ingress": "ingresses",
    "ingressclass": "ingressclasses",
    "networkpolicy": "networkpolicies",
    "horizontalpodautoscaler": "horizontalpodautoscalers",
    "poddisruptionbudget": "poddisruptionbudgets",
    "priorityclass": "priorityclasses",
    "storageclass": "storageclasses",
    "volumeattachment": "volumeattachments",
    "role": "roles",
    "rolebinding": "rolebindings",
    "clusterrole": "clusterroles",
    "clusterrolebinding": "clusterrolebindings",
    "customresourcedefinition": "customresourcedefinitions",
    "mutatingwebhookconfiguration": "mutatingwebhookconfigurations",
    "validatingwebhookconfiguration": "validatingwebhookconfigurations",
    "lease": "leases",
    "controllerrevision": "controllerrevisions",
    "certificatesigningrequest": "certificatesigningrequests",
}


def _parse_api_version(api_version: str) -> tuple[str, str]:
    """Split apiVersion into (group, version). Core API → ('', 'v1')."""
    if "/" in api_version:
        parts = api_version.split("/", 1)
        return parts[0], parts[1]
    return "", api_version


def _kind_to_plural(kind: str) -> str:
    """Convert a Kind to its plural resource name using a comprehensive mapping
    with fallback heuristics for unknown types."""
    kind_lower = kind.lower()

    # Check the comprehensive mapping first
    if kind_lower in _KIND_PLURAL_MAP:
        return _KIND_PLURAL_MAP[kind_lower]

    # Fallback heuristics for unknown resource types
    if kind_lower.endswith("ss"):
        # e.g. "ingress" -> "ingresses" (already in map, but just in case)
        return kind_lower + "es"
    if kind_lower.endswith("s"):
        return kind_lower + "es"
    if kind_lower.endswith("y"):
        return kind_lower[:-1] + "ies"
    if kind_lower.endswith("x") or kind_lower.endswith("sh") or kind_lower.endswith("ch"):
        return kind_lower + "es"
    return kind_lower + "s"
