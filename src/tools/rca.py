"""
Kubernetes MCP Server – Root Cause Analysis (RCA) Engine

Provides automated root-cause analysis for common Kubernetes failure patterns.
Inspects pods, events, nodes, and deployments to surface the probable cause of
issues.
"""

from __future__ import annotations

import logging
from typing import Any

from kubernetes import client as k8s_client

from src.connectors.cluster import connector
from src.models import ConditionDetail, RCAReport

logger = logging.getLogger(__name__)


# ── Public API ───────────────────────────────────────────────────────────────


def run_cluster_rca(cluster: str) -> RCAReport:
    """Run a full RCA scan across the cluster and return a report."""
    conditions: list[ConditionDetail] = []
    affected: set[str] = set()

    conditions.extend(_check_unhealthy_nodes(cluster, affected))
    conditions.extend(_check_failing_pods(cluster, affected))
    conditions.extend(_check_pending_pods(cluster, affected))
    conditions.extend(_check_deployment_issues(cluster, affected))
    conditions.extend(_check_warning_events(cluster, affected))
    conditions.extend(_check_resource_pressure(cluster, affected))

    # Build summary
    if not conditions:
        summary = f"Cluster '{cluster}' appears healthy – no issues detected."
        root_cause = "No issues found."
        recommendations = ["Continue monitoring."]
    else:
        severity_order = {"critical": 0, "error": 1, "warning": 2, "info": 3}
        conditions.sort(key=lambda c: severity_order.get(c.severity, 9))
        top = conditions[0]
        summary = (
            f"Cluster '{cluster}' has {len(conditions)} issue(s). "
            f"Most severe: {top.type} on {top.resource} – {top.message}"
        )
        root_cause, recommendations = _determine_root_cause(conditions)

    return RCAReport(
        cluster_name=cluster,
        summary=summary,
        conditions=conditions,
        probable_root_cause=root_cause,
        recommended_actions=recommendations,
        affected_resources=sorted(affected),
    )


def run_pod_rca(cluster: str, namespace: str, pod_name: str) -> RCAReport:
    """Run RCA focused on a specific pod."""
    api = connector.get_client(cluster)
    v1 = k8s_client.CoreV1Api(api)
    conditions: list[ConditionDetail] = []
    affected: set[str] = set()

    try:
        pod = v1.read_namespaced_pod(pod_name, namespace)
    except Exception as e:
        return RCAReport(
            cluster_name=cluster,
            summary=f"Cannot read pod {namespace}/{pod_name}: {e}",
            probable_root_cause=f"Pod not found or inaccessible: {e}",
            recommended_actions=["Verify the pod name and namespace."],
        )

    resource_id = f"pod/{namespace}/{pod_name}"
    affected.add(resource_id)

    # Check pod conditions
    if pod.status and pod.status.conditions:
        for cond in pod.status.conditions:
            if cond.status != "True" and cond.type in ("Ready", "Initialized", "ContainersReady"):
                conditions.append(ConditionDetail(
                    type=f"Pod{cond.type}False",
                    resource=resource_id,
                    namespace=namespace,
                    message=cond.message or f"{cond.type} is {cond.status}: {cond.reason}",
                    severity="warning",
                ))

    # Container statuses
    if pod.status and pod.status.container_statuses:
        for cs in pod.status.container_statuses:
            if cs.restart_count > 3:
                conditions.append(ConditionDetail(
                    type="HighRestartCount",
                    resource=resource_id,
                    namespace=namespace,
                    message=f"Container '{cs.name}' has {cs.restart_count} restarts",
                    severity="error",
                ))
            if cs.state and cs.state.waiting:
                conditions.append(ConditionDetail(
                    type=f"ContainerWaiting:{cs.state.waiting.reason}",
                    resource=resource_id,
                    namespace=namespace,
                    message=cs.state.waiting.message or cs.state.waiting.reason or "Container waiting",
                    severity="error" if cs.state.waiting.reason in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull") else "warning",
                ))
            if cs.state and cs.state.terminated and cs.state.terminated.exit_code != 0:
                conditions.append(ConditionDetail(
                    type="ContainerTerminatedError",
                    resource=resource_id,
                    namespace=namespace,
                    message=f"Container '{cs.name}' terminated with exit code {cs.state.terminated.exit_code}: {cs.state.terminated.reason}",
                    severity="error",
                ))

    # Related events
    events = v1.list_namespaced_event(
        namespace,
        field_selector=f"involvedObject.name={pod_name},type=Warning",
    )
    for ev in events.items:
        conditions.append(ConditionDetail(
            type=f"Event:{ev.reason}",
            resource=resource_id,
            namespace=namespace,
            message=ev.message or "",
            severity="warning",
            raw_data={"count": ev.count, "last_seen": str(ev.last_timestamp)},
        ))

    # Get logs tail for CrashLoopBackOff
    log_snippet = ""
    try:
        log_snippet = v1.read_namespaced_pod_log(
            pod_name, namespace, tail_lines=30, previous=True
        )
    except Exception:
        try:
            log_snippet = v1.read_namespaced_pod_log(
                pod_name, namespace, tail_lines=30
            )
        except Exception:
            pass

    root_cause, recommendations = _determine_root_cause(conditions)
    summary = f"Pod {namespace}/{pod_name} analysis: {len(conditions)} issue(s) found."
    if not conditions:
        summary = f"Pod {namespace}/{pod_name} appears healthy."
        root_cause = "No issues detected."
        recommendations = ["The pod is running normally."]

    report = RCAReport(
        cluster_name=cluster,
        summary=summary,
        conditions=conditions,
        probable_root_cause=root_cause,
        recommended_actions=recommendations,
        affected_resources=sorted(affected),
    )
    if log_snippet:
        report.timeline.append({"type": "logs_tail", "data": log_snippet})

    return report


def run_namespace_rca(cluster: str, namespace: str) -> RCAReport:
    """Run RCA across all workloads in a specific namespace."""
    api = connector.get_client(cluster)
    v1 = k8s_client.CoreV1Api(api)
    apps_v1 = k8s_client.AppsV1Api(api)
    conditions: list[ConditionDetail] = []
    affected: set[str] = set()

    # Pods
    pods = v1.list_namespaced_pod(namespace)
    for pod in pods.items:
        rid = f"pod/{namespace}/{pod.metadata.name}"
        if pod.status and pod.status.phase not in ("Running", "Succeeded"):
            conditions.append(ConditionDetail(
                type=f"PodPhase:{pod.status.phase}",
                resource=rid,
                namespace=namespace,
                message=f"Pod is in {pod.status.phase} phase",
                severity="warning" if pod.status.phase == "Pending" else "error",
            ))
            affected.add(rid)

        if pod.status and pod.status.container_statuses:
            for cs in pod.status.container_statuses:
                if cs.restart_count > 3:
                    conditions.append(ConditionDetail(
                        type="HighRestartCount",
                        resource=rid,
                        namespace=namespace,
                        message=f"Container '{cs.name}' has {cs.restart_count} restarts",
                        severity="error",
                    ))
                    affected.add(rid)

    # Deployments
    deps = apps_v1.list_namespaced_deployment(namespace)
    for dep in deps.items:
        rid = f"deployment/{namespace}/{dep.metadata.name}"
        desired = dep.spec.replicas or 0
        ready = dep.status.ready_replicas or 0
        if ready < desired:
            conditions.append(ConditionDetail(
                type="DeploymentUnderReplicated",
                resource=rid,
                namespace=namespace,
                message=f"Desired {desired} replicas, only {ready} ready",
                severity="warning",
            ))
            affected.add(rid)

    # Events
    events = v1.list_namespaced_event(namespace, field_selector="type=Warning")
    for ev in events.items:
        obj = f"{ev.involved_object.kind}/{namespace}/{ev.involved_object.name}" if ev.involved_object else "unknown"
        conditions.append(ConditionDetail(
            type=f"Event:{ev.reason}",
            resource=obj,
            namespace=namespace,
            message=ev.message or "",
            severity="warning",
        ))
        affected.add(obj)

    root_cause, recommendations = _determine_root_cause(conditions)
    summary = f"Namespace '{namespace}' on '{cluster}': {len(conditions)} issue(s)."
    if not conditions:
        summary = f"Namespace '{namespace}' on '{cluster}' is healthy."

    return RCAReport(
        cluster_name=cluster,
        summary=summary,
        conditions=conditions,
        probable_root_cause=root_cause,
        recommended_actions=recommendations,
        affected_resources=sorted(affected),
    )


# ── Private Checks ──────────────────────────────────────────────────────────


def _check_unhealthy_nodes(cluster: str, affected: set[str]) -> list[ConditionDetail]:
    """Check for nodes that are NotReady or have conditions like MemoryPressure."""
    results = []
    try:
        v1 = k8s_client.CoreV1Api(connector.get_client(cluster))
        nodes = v1.list_node()
        for node in nodes.items:
            nid = f"node/{node.metadata.name}"
            for cond in (node.status.conditions or []):
                if cond.type == "Ready" and cond.status != "True":
                    results.append(ConditionDetail(
                        type="NodeNotReady",
                        resource=nid,
                        message=cond.message or f"Node not ready: {cond.reason}",
                        severity="critical",
                    ))
                    affected.add(nid)
                elif cond.type in ("MemoryPressure", "DiskPressure", "PIDPressure") and cond.status == "True":
                    results.append(ConditionDetail(
                        type=f"Node{cond.type}",
                        resource=nid,
                        message=cond.message or f"{cond.type} detected",
                        severity="error",
                    ))
                    affected.add(nid)
    except Exception as e:
        logger.warning("Failed to check nodes on %s: %s", cluster, e)
    return results


def _check_failing_pods(cluster: str, affected: set[str]) -> list[ConditionDetail]:
    """Check for pods in CrashLoopBackOff or Error state."""
    results = []
    try:
        v1 = k8s_client.CoreV1Api(connector.get_client(cluster))
        pods = v1.list_pod_for_all_namespaces()
        for pod in pods.items:
            pid = f"pod/{pod.metadata.namespace}/{pod.metadata.name}"
            if pod.status and pod.status.container_statuses:
                for cs in pod.status.container_statuses:
                    if cs.state and cs.state.waiting and cs.state.waiting.reason in (
                        "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull",
                        "CreateContainerConfigError", "RunContainerError",
                    ):
                        results.append(ConditionDetail(
                            type=cs.state.waiting.reason,
                            resource=pid,
                            namespace=pod.metadata.namespace,
                            message=cs.state.waiting.message or cs.state.waiting.reason,
                            severity="error",
                        ))
                        affected.add(pid)
    except Exception as e:
        logger.warning("Failed to check pods on %s: %s", cluster, e)
    return results


def _check_pending_pods(cluster: str, affected: set[str]) -> list[ConditionDetail]:
    """Check for pods stuck in Pending state."""
    results = []
    try:
        v1 = k8s_client.CoreV1Api(connector.get_client(cluster))
        pods = v1.list_pod_for_all_namespaces(field_selector="status.phase=Pending")
        for pod in pods.items:
            pid = f"pod/{pod.metadata.namespace}/{pod.metadata.name}"
            results.append(ConditionDetail(
                type="PodPending",
                resource=pid,
                namespace=pod.metadata.namespace,
                message=f"Pod pending – may be unschedulable or resource-constrained",
                severity="warning",
            ))
            affected.add(pid)
    except Exception as e:
        logger.warning("Failed to check pending pods on %s: %s", cluster, e)
    return results


def _check_deployment_issues(cluster: str, affected: set[str]) -> list[ConditionDetail]:
    """Check for deployments with unavailable replicas."""
    results = []
    try:
        apps = k8s_client.AppsV1Api(connector.get_client(cluster))
        deps = apps.list_deployment_for_all_namespaces()
        for dep in deps.items:
            desired = dep.spec.replicas or 0
            available = dep.status.available_replicas or 0
            if desired > 0 and available < desired:
                did = f"deployment/{dep.metadata.namespace}/{dep.metadata.name}"
                results.append(ConditionDetail(
                    type="DeploymentUnderReplicated",
                    resource=did,
                    namespace=dep.metadata.namespace,
                    message=f"Desired {desired}, available {available}",
                    severity="warning" if available > 0 else "error",
                ))
                affected.add(did)
    except Exception as e:
        logger.warning("Failed to check deployments on %s: %s", cluster, e)
    return results


def _check_warning_events(cluster: str, affected: set[str]) -> list[ConditionDetail]:
    """Gather recent warning events as symptoms."""
    results = []
    try:
        v1 = k8s_client.CoreV1Api(connector.get_client(cluster))
        events = v1.list_event_for_all_namespaces(
            field_selector="type=Warning", limit=50
        )
        seen: set[str] = set()
        for ev in events.items:
            key = f"{ev.reason}:{ev.involved_object.kind}/{ev.involved_object.name}" if ev.involved_object else ev.reason
            if key in seen:
                continue
            seen.add(key)
            obj = f"{ev.involved_object.kind}/{ev.metadata.namespace}/{ev.involved_object.name}" if ev.involved_object else "unknown"
            results.append(ConditionDetail(
                type=f"Event:{ev.reason}",
                resource=obj,
                namespace=ev.metadata.namespace,
                message=ev.message or "",
                severity="warning",
                raw_data={"count": ev.count, "last_seen": str(ev.last_timestamp)},
            ))
            affected.add(obj)
    except Exception as e:
        logger.warning("Failed to check events on %s: %s", cluster, e)
    return results


def _check_resource_pressure(cluster: str, affected: set[str]) -> list[ConditionDetail]:
    """Check if any nodes have resource pressure conditions."""
    # Already covered in _check_unhealthy_nodes – placeholder for future
    # metrics-server integration (CPU/memory utilization thresholds)
    return []


# ── Root Cause Determination ─────────────────────────────────────────────────


def _determine_root_cause(conditions: list[ConditionDetail]) -> tuple[str, list[str]]:
    """Analyze conditions and determine the probable root cause."""
    if not conditions:
        return "No issues detected.", ["Continue normal monitoring."]

    type_counts: dict[str, int] = {}
    for c in conditions:
        base_type = c.type.split(":")[0]
        type_counts[base_type] = type_counts.get(base_type, 0) + 1

    recommendations: list[str] = []
    root_cause_parts: list[str] = []

    if "NodeNotReady" in type_counts:
        root_cause_parts.append(f"{type_counts['NodeNotReady']} node(s) are not ready")
        recommendations.extend([
            "Check kubelet status on affected nodes: systemctl status kubelet",
            "Check node resources: kubectl describe node <name>",
            "Review kubelet logs: journalctl -u kubelet -f",
        ])

    if "NodeMemoryPressure" in type_counts or "NodeDiskPressure" in type_counts:
        root_cause_parts.append("Node(s) under resource pressure")
        recommendations.extend([
            "Free disk space or add storage to affected nodes",
            "Consider adding more nodes to distribute workload",
            "Review pod resource requests and limits",
        ])

    if "CrashLoopBackOff" in type_counts:
        root_cause_parts.append(f"{type_counts['CrashLoopBackOff']} container(s) in CrashLoopBackOff")
        recommendations.extend([
            "Check pod logs: kubectl logs <pod> --previous",
            "Review application configuration (ConfigMaps, Secrets, env vars)",
            "Verify container image exists and is pullable",
            "Check resource limits – OOMKilled containers need more memory",
        ])

    if "ImagePullBackOff" in type_counts or "ErrImagePull" in type_counts:
        root_cause_parts.append("Image pull failures detected")
        recommendations.extend([
            "Verify image name and tag are correct",
            "Check image registry authentication (imagePullSecrets)",
            "Ensure network connectivity to container registry",
        ])

    if "PodPending" in type_counts:
        root_cause_parts.append(f"{type_counts['PodPending']} pod(s) stuck in Pending state")
        recommendations.extend([
            "Check node resources – pods may be unschedulable due to resource constraints",
            "Review node taints and pod tolerations",
            "Check PersistentVolumeClaim bindings",
            "Consider scaling up cluster nodes",
        ])

    if "DeploymentUnderReplicated" in type_counts:
        root_cause_parts.append(f"{type_counts['DeploymentUnderReplicated']} deployment(s) under-replicated")
        recommendations.extend([
            "Check pod statuses in the deployment",
            "Verify cluster has enough resources for requested replicas",
        ])

    if "HighRestartCount" in type_counts:
        root_cause_parts.append("High container restart counts detected")
        recommendations.extend([
            "Investigate why containers are restarting (check logs, health probes)",
            "Review liveness and readiness probe configurations",
        ])

    if not root_cause_parts:
        root_cause_parts.append("Warning events detected – review conditions for details")
        recommendations.append("Investigate the warning events listed in the conditions.")

    root_cause = "; ".join(root_cause_parts)
    # Deduplicate recommendations
    seen: set[str] = set()
    unique_recs = []
    for r in recommendations:
        if r not in seen:
            seen.add(r)
            unique_recs.append(r)

    return root_cause, unique_recs
