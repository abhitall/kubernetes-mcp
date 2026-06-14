"""
Kubernetes MCP Server – Token-Efficient / Consolidated Operations

These tools fold what would otherwise be several agent round-trips (list → get →
logs → events) into a single high-signal call. They implement, for Kubernetes
operations, the efficiency guidance Anthropic published for agent tooling:

* **Consolidate related operations into high-impact tools.** Instead of forcing
  an agent to chain ``get_pod`` + ``describe`` + ``get_pod_logs`` +
  ``list_events``, expose one ``get_pod_context`` that compiles the relevant
  state at once — the "``get_customer_context``" pattern.
  → https://www.anthropic.com/engineering/writing-tools-for-agents
* **Return the smallest set of high-signal tokens.** Responses are ``concise``
  by default (status, reasons, problem signals) and only return the full object
  graph when ``response_format="detailed"`` is requested.
  → https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
* **Filter / transform data before it reaches the model.** ``project_resource``
  returns only the requested dotted-path fields rather than the whole object,
  mirroring the "filter in the execution environment" idea from code execution.
  → https://www.anthropic.com/engineering/code-execution-with-mcp
* **Avoid chaining individual calls.** ``batch_read`` runs many read operations
  in a single round-trip, the safe analogue of in-code control flow.

All operations here are strictly read-only and therefore independent of the
server's read-only mode.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from kubernetes.client.rest import ApiException

from src.tools import k8s_ops
from src.tools.k8s_ops import _age, _apps, _core, _safe_annotations, _serialize

logger = logging.getLogger(__name__)

CONCISE = "concise"
DETAILED = "detailed"

# Pod phases that represent healthy / non-actionable states.
_HEALTHY_PHASES = {"Running", "Succeeded"}


# ── Shared summarizers ───────────────────────────────────────────────────────


def _container_status_summary(pod) -> list[dict[str, Any]]:
    """Per-container status with the *reason* fields that actually explain a
    failure (Waiting/Terminated reasons, exit codes, last-terminated state).

    This is the highest-signal part of a pod for debugging and is what an agent
    would otherwise dig out of the full object."""
    out: list[dict[str, Any]] = []
    statuses = (pod.status.container_statuses or []) if pod.status else []
    for cs in statuses:
        entry: dict[str, Any] = {
            "name": cs.name,
            "ready": cs.ready,
            "restarts": cs.restart_count or 0,
            "image": cs.image,
        }
        state = cs.state
        if state and state.waiting:
            entry["state"] = "waiting"
            entry["reason"] = state.waiting.reason
            if state.waiting.message:
                entry["message"] = state.waiting.message
        elif state and state.terminated:
            entry["state"] = "terminated"
            entry["reason"] = state.terminated.reason
            entry["exit_code"] = state.terminated.exit_code
        elif state and state.running:
            entry["state"] = "running"
        # last_state.terminated is the smoking gun for CrashLoopBackOff
        if cs.last_state and cs.last_state.terminated:
            entry["last_terminated_reason"] = cs.last_state.terminated.reason
            entry["last_exit_code"] = cs.last_state.terminated.exit_code
        out.append(entry)
    return out


def _pod_is_problem(pod_summary: dict[str, Any]) -> bool:
    """Heuristic: is this pod worth the agent's attention?"""
    if pod_summary.get("phase") not in _HEALTHY_PHASES:
        return True
    if pod_summary.get("restarts", 0) > 0:
        return True
    ready = pod_summary.get("ready", "")
    if isinstance(ready, str) and "/" in ready:
        have, want = ready.split("/", 1)
        if have != want:
            return True
    return False


def _recent_events(cluster: str, namespace: str, *, name: str | None = None,
                   kind: str | None = None, only_warnings: bool = False,
                   limit: int = 20) -> list[dict[str, Any]]:
    """Concise, most-recent-first events, optionally scoped to one object."""
    core = _core(cluster)
    selectors: list[str] = []
    if name:
        selectors.append(f"involvedObject.name={name}")
    if kind:
        selectors.append(f"involvedObject.kind={kind}")
    if only_warnings:
        selectors.append("type=Warning")
    kwargs: dict[str, Any] = {"limit": max(limit, 1)}
    if selectors:
        kwargs["field_selector"] = ",".join(selectors)
    try:
        ev = core.list_namespaced_event(namespace, **kwargs)
    except ApiException as exc:
        return [{"error": str(exc)}]

    def _sort_key(e):
        ts = e.last_timestamp or e.event_time or e.metadata.creation_timestamp
        return ts.timestamp() if ts is not None else 0.0

    items = sorted(ev.items, key=_sort_key, reverse=True)[:limit]
    return [{
        "type": e.type,
        "reason": e.reason,
        "message": e.message,
        "count": e.count,
        "object": f"{e.involved_object.kind}/{e.involved_object.name}" if e.involved_object else "",
        "age": _age(e.last_timestamp or e.metadata.creation_timestamp),
    } for e in items]


# ── get_pod_context (replaces get_pod + describe + logs + events) ─────────────


def get_pod_context(cluster: str, namespace: str, name: str,
                    tail_lines: int = 50, include_logs: bool = True,
                    response_format: str = CONCISE) -> dict[str, Any]:
    """Compile everything needed to triage a single pod in one call.

    Folds ``get_pod`` + ``describe`` + ``get_pod_logs`` (+ previous logs for
    restarting containers) + scoped events into a single high-signal response.
    Logs for crash-looping containers automatically include the previous
    instance, which is where the failure usually is.
    """
    core = _core(cluster)
    pod = core.read_namespaced_pod(name, namespace)

    containers = _container_status_summary(pod)
    restarts = sum(c.get("restarts", 0) for c in containers)
    total = len(pod.spec.containers) if pod.spec and pod.spec.containers else 0
    ready = sum(1 for c in containers if c.get("ready"))

    owners = [
        {"kind": o.kind, "name": o.name}
        for o in (pod.metadata.owner_references or [])
    ]

    summary: dict[str, Any] = {
        "name": pod.metadata.name,
        "namespace": pod.metadata.namespace,
        "phase": pod.status.phase if pod.status else "Unknown",
        "ready": f"{ready}/{total}",
        "restarts": restarts,
        "node": pod.spec.node_name if pod.spec else None,
        "pod_ip": pod.status.pod_ip if pod.status else None,
        "qos_class": (pod.status.qos_class if pod.status else None) or "Unknown",
        "age": _age(pod.metadata.creation_timestamp),
        "owners": owners,
        "containers": containers,
        "conditions": [
            {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
            for c in (pod.status.conditions or [])
        ] if pod.status else [],
    }

    logs: dict[str, str] = {}
    if include_logs:
        for c in containers:
            cname = c["name"]
            try:
                logs[cname] = k8s_ops.get_pod_logs(
                    cluster, namespace, name, container=cname, tail_lines=tail_lines,
                )
            except Exception as exc:  # noqa: BLE001 - surface as data, not failure
                logs[cname] = f"<error fetching logs: {exc}>"
            if c.get("restarts", 0) > 0:
                try:
                    logs[f"{cname} (previous)"] = k8s_ops.get_pod_logs(
                        cluster, namespace, name, container=cname,
                        tail_lines=tail_lines, previous=True,
                    )
                except Exception:  # noqa: BLE001 - previous logs may not exist
                    pass

    result: dict[str, Any] = {
        "pod": summary,
        "events": _recent_events(cluster, namespace, name=name, kind="Pod", limit=20),
    }
    if include_logs:
        result["logs"] = logs
    if response_format == DETAILED:
        result["raw"] = _serialize(pod)
    return result


# ── namespace_overview (replaces list_pods + list_deployments + events + …) ───


def namespace_overview(cluster: str, namespace: str) -> dict[str, Any]:
    """One-call triage of a namespace: pod phase counts, the specific pods that
    need attention, under-provisioned workloads, recent warnings, and storage
    issues — instead of five separate list calls the agent has to filter itself.
    """
    pods = k8s_ops.list_pods(cluster, namespace)
    phase_counts = Counter(p.get("phase", "Unknown") for p in pods)
    problem_pods = [
        {
            "name": p["name"],
            "phase": p["phase"],
            "ready": p["ready"],
            "restarts": p["restarts"],
        }
        for p in pods if _pod_is_problem(p)
    ]

    deployments = k8s_ops.list_deployments(cluster, namespace)
    unhealthy_deployments = [
        {
            "name": d["name"],
            "ready": f"{d.get('ready_replicas', 0)}/{d.get('replicas', 0)}",
        }
        for d in deployments
        if (d.get("ready_replicas") or 0) < (d.get("replicas") or 0)
    ]

    try:
        pvcs = k8s_ops.list_pvcs(cluster, namespace)
        unbound_pvcs = [
            {"name": v["name"], "status": v["status"]}
            for v in pvcs if v.get("status") != "Bound"
        ]
    except Exception as exc:  # noqa: BLE001
        unbound_pvcs = [{"error": str(exc)}]

    try:
        service_count = len(k8s_ops.list_services(cluster, namespace))
    except Exception:  # noqa: BLE001
        service_count = -1

    return {
        "namespace": namespace,
        "cluster": cluster,
        "pods": {
            "total": len(pods),
            "by_phase": dict(phase_counts),
            "problem_pods": problem_pods,
        },
        "deployments": {
            "total": len(deployments),
            "unhealthy": unhealthy_deployments,
        },
        "services": service_count,
        "unbound_pvcs": unbound_pvcs,
        "recent_warnings": _recent_events(
            cluster, namespace, only_warnings=True, limit=20
        ),
    }


# ── get_deployment_context (deployment + rollout + pods + events) ─────────────


def get_deployment_context(cluster: str, namespace: str, name: str,
                           response_format: str = CONCISE) -> dict[str, Any]:
    """Deployment health in one call: spec/status summary, rollout progress,
    the pods it owns (concise), and recent events — instead of ``get_deployment``
    + ``get_rollout_status`` + ``list_pods`` + ``list_events``."""
    dep = _apps(cluster).read_namespaced_deployment(name, namespace)
    rollout = k8s_ops.get_rollout_status(cluster, namespace, name)

    pods: list[dict[str, Any]] = []
    match_labels = (
        dep.spec.selector.match_labels
        if dep.spec and dep.spec.selector and dep.spec.selector.match_labels
        else {}
    )
    if match_labels:
        label_str = ",".join(f"{k}={v}" for k, v in match_labels.items())
        pod_list = k8s_ops.list_pods(cluster, namespace, label_selector=label_str)
        pods = [
            {
                "name": p["name"],
                "phase": p["phase"],
                "ready": p["ready"],
                "restarts": p["restarts"],
                "node": p["node"],
            }
            for p in pod_list
        ]

    result: dict[str, Any] = {
        "deployment": {
            "name": dep.metadata.name,
            "namespace": dep.metadata.namespace,
            "replicas": dep.spec.replicas if dep.spec else None,
            "ready_replicas": dep.status.ready_replicas or 0 if dep.status else 0,
            "available_replicas": dep.status.available_replicas or 0 if dep.status else 0,
            "strategy": dep.spec.strategy.type if dep.spec and dep.spec.strategy else None,
            "images": k8s_ops._container_images(dep.spec.template.spec)
            if dep.spec and dep.spec.template and dep.spec.template.spec else [],
            "annotations": _safe_annotations(dep),
        },
        "rollout": rollout,
        "pods": pods,
        "events": _recent_events(cluster, namespace, name=name, kind="Deployment", limit=15),
    }
    if response_format == DETAILED:
        result["raw"] = _serialize(dep)
    return result


# ── cluster_overview (light, single-call cluster posture) ─────────────────────


def cluster_overview(cluster: str) -> dict[str, Any]:
    """A light, single-call snapshot of cluster posture: node readiness, problem
    nodes, namespace count, and the most recent cluster-wide warnings. Avoids
    the expensive all-pod listing that the full health check performs."""
    nodes = k8s_ops.list_nodes(cluster)
    ready_nodes = sum(1 for n in nodes if n.get("ready") == "True")
    problem_nodes = [
        {
            "name": n["name"],
            "ready": n["ready"],
            "unschedulable": n.get("unschedulable", False),
            "taints": [t.get("key") for t in n.get("taints", [])],
        }
        for n in nodes
        if n.get("ready") != "True" or n.get("unschedulable")
    ]

    try:
        ns_count = len(k8s_ops.list_namespaces(cluster))
    except Exception:  # noqa: BLE001
        ns_count = -1

    return {
        "cluster": cluster,
        "nodes": {
            "total": len(nodes),
            "ready": ready_nodes,
            "problem_nodes": problem_nodes,
        },
        "namespaces": ns_count,
        "recent_warnings": _recent_events_all(cluster, only_warnings=True, limit=30),
    }


def _recent_events_all(cluster: str, *, only_warnings: bool = False,
                       limit: int = 30) -> list[dict[str, Any]]:
    """Cluster-wide recent events (all namespaces)."""
    core = _core(cluster)
    kwargs: dict[str, Any] = {"limit": max(limit, 1)}
    if only_warnings:
        kwargs["field_selector"] = "type=Warning"
    try:
        ev = core.list_event_for_all_namespaces(**kwargs)
    except ApiException as exc:
        return [{"error": str(exc)}]

    def _sort_key(e):
        ts = e.last_timestamp or e.event_time or e.metadata.creation_timestamp
        return ts.timestamp() if ts is not None else 0.0

    items = sorted(ev.items, key=_sort_key, reverse=True)[:limit]
    return [{
        "type": e.type,
        "reason": e.reason,
        "message": e.message,
        "namespace": e.metadata.namespace,
        "object": f"{e.involved_object.kind}/{e.involved_object.name}" if e.involved_object else "",
        "age": _age(e.last_timestamp or e.metadata.creation_timestamp),
    } for e in items]


# ── project_resource (return only requested fields) ──────────────────────────


def _dig(obj: Any, dotted: str) -> Any:
    """Resolve a dotted path like ``status.containerStatuses[0].ready`` against a
    nested dict/list. Returns None if any segment is missing."""
    cur = obj
    for raw in dotted.split("."):
        key = raw
        index: int | None = None
        if "[" in raw and raw.endswith("]"):
            key, idx = raw[:-1].split("[", 1)
            try:
                index = int(idx)
            except ValueError:
                return None
        if key:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                return None
        if index is not None:
            if isinstance(cur, list) and -len(cur) <= index < len(cur):
                cur = cur[index]
            else:
                return None
    return cur


def project_resource(cluster: str, api_version: str, kind: str, name: str,
                     fields: list[str], namespace: str | None = None) -> dict[str, Any]:
    """Fetch a resource but return only the requested dotted-path fields.

    Example ``fields``: ``["status.phase", "spec.nodeName",
    "status.containerStatuses[0].restartCount"]``. Keeps large objects out of
    the model context by filtering server-side."""
    obj = k8s_ops.get_resource(cluster, api_version, kind, name, namespace)
    if isinstance(obj, dict) and "error" in obj:
        return obj
    return {f: _dig(obj, f) for f in fields}


# ── batch_read (many reads, one round-trip) ──────────────────────────────────

# Whitelist of read-only operations callable via batch_read. Mutating
# operations are deliberately excluded.
_BATCH_OPS = {
    "list_namespaces": k8s_ops.list_namespaces,
    "list_pods": k8s_ops.list_pods,
    "get_pod": k8s_ops.get_pod,
    "get_pod_logs": k8s_ops.get_pod_logs,
    "top_pods": k8s_ops.top_pods,
    "list_deployments": k8s_ops.list_deployments,
    "get_deployment": k8s_ops.get_deployment,
    "get_rollout_status": k8s_ops.get_rollout_status,
    "list_nodes": k8s_ops.list_nodes,
    "top_nodes": k8s_ops.top_nodes,
    "list_events": k8s_ops.list_events,
    "list_services": k8s_ops.list_services,
    "list_configmaps": k8s_ops.list_configmaps,
    "get_configmap": k8s_ops.get_configmap,
    "list_secrets": k8s_ops.list_secrets,
    "list_statefulsets": k8s_ops.list_statefulsets,
    "list_daemonsets": k8s_ops.list_daemonsets,
    "list_jobs": k8s_ops.list_jobs,
    "list_cronjobs": k8s_ops.list_cronjobs,
    "list_ingresses": k8s_ops.list_ingresses,
    "list_pvcs": k8s_ops.list_pvcs,
    "list_hpas": k8s_ops.list_hpas,
    "list_resources": k8s_ops.list_resources,
    "get_resource": k8s_ops.get_resource,
    "describe_resource": k8s_ops.describe_resource,
    # consolidated read tools
    "get_pod_context": get_pod_context,
    "namespace_overview": namespace_overview,
    "get_deployment_context": get_deployment_context,
    "cluster_overview": cluster_overview,
    "project_resource": project_resource,
}


def batch_read(cluster: str, operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run several read-only operations against one cluster in a single call.

    Each operation is ``{"op": "<name>", "args": {...}}`` where ``op`` is one of
    the whitelisted read operations and ``args`` are its keyword arguments
    (excluding ``cluster``, which is injected). Returns one result entry per
    operation; a failure in one does not abort the others.
    """
    results: list[dict[str, Any]] = []
    for i, spec in enumerate(operations):
        op = spec.get("op")
        args = spec.get("args", {}) or {}
        fn = _BATCH_OPS.get(op)
        if fn is None:
            results.append({
                "op": op,
                "ok": False,
                "error": f"unknown or non-readonly op '{op}'. "
                         f"Allowed: {sorted(_BATCH_OPS)}",
            })
            continue
        try:
            results.append({"op": op, "ok": True, "result": fn(cluster, **args)})
        except Exception as exc:  # noqa: BLE001 - report per-op, keep going
            results.append({"op": op, "ok": False, "error": str(exc)})
    return results
