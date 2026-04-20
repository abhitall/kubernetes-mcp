"""
Kubernetes MCP Server – Self-Healing Engine

Provides automated remediation actions based on RCA reports.  Supports dry-run
mode and approval workflows before executing destructive actions.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from kubernetes import client as k8s_client

from src.connectors.cluster import connector
from src.models import (
    HealAction,
    HealPlan,
    HealResult,
    RCAReport,
)

logger = logging.getLogger(__name__)


# ── Public API ───────────────────────────────────────────────────────────────


def generate_heal_plan(
    rca: RCAReport,
    *,
    dry_run: bool = True,
    requires_approval: bool = True,
) -> HealPlan:
    """Analyse an RCA report and generate a healing plan with actions."""
    actions: list[HealAction] = []

    for condition in rca.conditions:
        new_actions = _actions_for_condition(condition.type, condition.resource, condition.namespace)
        actions.extend(new_actions)

    # Deduplicate by (action_type, target_resource)
    seen: set[str] = set()
    unique: list[HealAction] = []
    for a in actions:
        key = f"{a.action_type}:{a.target_resource}"
        if key not in seen:
            seen.add(key)
            unique.append(a)

    return HealPlan(
        cluster_name=rca.cluster_name,
        rca_summary=rca.probable_root_cause,
        actions=unique,
        dry_run=dry_run,
        requires_approval=requires_approval,
    )


def execute_heal_plan(
    cluster: str,
    plan: HealPlan,
    *,
    force: bool = False,
) -> list[HealResult]:
    """Execute a healing plan.  Skips execution if the plan requires approval
    and *force* is False."""
    if plan.requires_approval and not force:
        return [
            HealResult(
                action=a,
                success=False,
                message="Skipped – plan requires approval.  Pass force=True to execute.",
            )
            for a in plan.actions
        ]

    results: list[HealResult] = []
    for action in plan.actions:
        result = _execute_action(cluster, action, dry_run=plan.dry_run)
        results.append(result)

    return results


def quick_heal(
    cluster: str,
    action_type: str,
    resource: str,
    namespace: str | None = None,
    parameters: dict[str, Any] | None = None,
    *,
    dry_run: bool = False,
) -> HealResult:
    """Execute a single healing action directly (bypasses plan/approval)."""
    action = HealAction(
        action_type=action_type,
        target_resource=resource,
        namespace=namespace,
        parameters=parameters or {},
        description=f"Quick heal: {action_type} on {resource}",
    )
    return _execute_action(cluster, action, dry_run=dry_run)


# ── Action Generation ────────────────────────────────────────────────────────


def _actions_for_condition(condition_type: str, resource: str, namespace: str | None) -> list[HealAction]:
    """Map a condition type to a list of healing actions."""
    actions: list[HealAction] = []
    base_type = condition_type.split(":")[0]

    if base_type == "CrashLoopBackOff":
        actions.append(HealAction(
            action_type="restart_pod",
            target_resource=resource,
            namespace=namespace,
            description="Delete pod to get a fresh restart (CrashLoopBackOff remediation)",
        ))

    elif base_type == "HighRestartCount":
        actions.append(HealAction(
            action_type="restart_pod",
            target_resource=resource,
            namespace=namespace,
            description="Restart pod due to high restart count",
        ))

    elif base_type in ("ImagePullBackOff", "ErrImagePull"):
        actions.append(HealAction(
            action_type="restart_pod",
            target_resource=resource,
            namespace=namespace,
            description="Delete pod to retry image pull",
        ))

    elif base_type == "PodPending":
        # Cannot restart a pending pod – suggest scaling down/up the parent deployment
        actions.append(HealAction(
            action_type="rollout_restart",
            target_resource=resource,
            namespace=namespace,
            description="Rollout restart the owning deployment to re-schedule",
        ))

    elif base_type == "DeploymentUnderReplicated":
        actions.append(HealAction(
            action_type="rollout_restart",
            target_resource=resource,
            namespace=namespace,
            description="Rollout restart deployment to recover missing replicas",
        ))

    elif base_type == "NodeNotReady":
        actions.append(HealAction(
            action_type="cordon_node",
            target_resource=resource,
            namespace=None,
            description="Cordon unhealthy node to prevent new scheduling",
        ))
        actions.append(HealAction(
            action_type="drain_node",
            target_resource=resource,
            namespace=None,
            description="Drain unhealthy node to reschedule workloads",
        ))

    elif base_type in ("NodeMemoryPressure", "NodeDiskPressure", "NodePIDPressure"):
        actions.append(HealAction(
            action_type="cordon_node",
            target_resource=resource,
            namespace=None,
            description=f"Cordon node under {base_type}",
        ))

    elif base_type == "ContainerTerminatedError":
        actions.append(HealAction(
            action_type="restart_pod",
            target_resource=resource,
            namespace=namespace,
            description="Restart pod after container terminated with error",
        ))

    elif base_type.startswith("Event"):
        # Generic events – no automatic action, informational only
        pass

    return actions


# ── Action Execution ─────────────────────────────────────────────────────────


def _execute_action(cluster: str, action: HealAction, *, dry_run: bool) -> HealResult:
    """Execute a single healing action on the given cluster."""
    started = datetime.now(timezone.utc)
    try:
        if dry_run:
            return HealResult(
                action=action,
                success=True,
                message=f"[DRY RUN] Would execute {action.action_type} on {action.target_resource}",
                timestamp=started.isoformat(),
            )

        executor = _EXECUTORS.get(action.action_type)
        if not executor:
            return HealResult(
                action=action,
                success=False,
                message=f"Unknown action type: {action.action_type}",
                timestamp=started.isoformat(),
            )

        msg = executor(cluster, action)
        return HealResult(
            action=action,
            success=True,
            message=msg,
            timestamp=started.isoformat(),
        )

    except Exception as e:
        logger.exception("Heal action failed: %s on %s", action.action_type, action.target_resource)
        return HealResult(
            action=action,
            success=False,
            message=str(e),
            timestamp=started.isoformat(),
        )


# ── Individual Executors ─────────────────────────────────────────────────────


def _exec_restart_pod(cluster: str, action: HealAction) -> str:
    """Delete the pod so its controller recreates it."""
    parts = action.target_resource.split("/")
    # resource format: pod/<namespace>/<name>
    if len(parts) >= 3:
        _, ns, name = parts[0], parts[1], parts[2]
    else:
        ns = action.namespace or "default"
        name = parts[-1]

    v1 = k8s_client.CoreV1Api(connector.get_client(cluster))
    v1.delete_namespaced_pod(name, ns, grace_period_seconds=30)
    return f"Pod {ns}/{name} deleted for restart."


def _exec_rollout_restart(cluster: str, action: HealAction) -> str:
    """Perform a rollout restart by patching the deployment's template annotation."""
    parts = action.target_resource.split("/")
    if len(parts) >= 3:
        _, ns, name = parts[0], parts[1], parts[2]
    elif len(parts) == 2:
        ns, name = parts
    else:
        ns = action.namespace or "default"
        name = parts[-1]

    apps = k8s_client.AppsV1Api(connector.get_client(cluster))
    now = datetime.now(timezone.utc).isoformat()
    patch = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubernetes-mcp/restartedAt": now,
                    }
                }
            }
        }
    }
    apps.patch_namespaced_deployment(name, ns, patch)
    return f"Deployment {ns}/{name} rollout restart triggered."


def _exec_scale_deployment(cluster: str, action: HealAction) -> str:
    """Scale a deployment to the desired replica count."""
    parts = action.target_resource.split("/")
    if len(parts) >= 3:
        _, ns, name = parts[0], parts[1], parts[2]
    else:
        ns = action.namespace or "default"
        name = parts[-1]

    replicas = action.parameters.get("replicas", 1) if action.parameters else 1
    apps = k8s_client.AppsV1Api(connector.get_client(cluster))
    apps.patch_namespaced_deployment_scale(name, ns, {"spec": {"replicas": replicas}})
    return f"Deployment {ns}/{name} scaled to {replicas} replicas."


def _exec_cordon_node(cluster: str, action: HealAction) -> str:
    """Mark a node as unschedulable."""
    parts = action.target_resource.split("/")
    name = parts[-1]

    v1 = k8s_client.CoreV1Api(connector.get_client(cluster))
    v1.patch_node(name, {"spec": {"unschedulable": True}})
    return f"Node {name} cordoned."


def _exec_drain_node(cluster: str, action: HealAction) -> str:
    """Drain a node by evicting all pods (best-effort)."""
    parts = action.target_resource.split("/")
    name = parts[-1]

    v1 = k8s_client.CoreV1Api(connector.get_client(cluster))

    # Cordon first
    v1.patch_node(name, {"spec": {"unschedulable": True}})

    # List non-daemonset pods on the node
    pods = v1.list_pod_for_all_namespaces(field_selector=f"spec.nodeName={name}")
    evicted = 0
    for pod in pods.items:
        # Skip mirror pods and DaemonSet pods
        if pod.metadata.annotations and "kubernetes.io/config.mirror" in pod.metadata.annotations:
            continue
        if pod.metadata.owner_references:
            if any(ref.kind == "DaemonSet" for ref in pod.metadata.owner_references):
                continue
        try:
            eviction = k8s_client.V1Eviction(
                metadata=k8s_client.V1ObjectMeta(
                    name=pod.metadata.name,
                    namespace=pod.metadata.namespace,
                ),
                delete_options=k8s_client.V1DeleteOptions(grace_period_seconds=30),
            )
            v1.create_namespaced_pod_eviction(
                pod.metadata.name, pod.metadata.namespace, eviction
            )
            evicted += 1
        except Exception as e:
            logger.warning("Failed to evict pod %s/%s: %s", pod.metadata.namespace, pod.metadata.name, e)

    return f"Node {name} drained – {evicted} pod(s) evicted."


def _exec_uncordon_node(cluster: str, action: HealAction) -> str:
    """Mark a node as schedulable again."""
    parts = action.target_resource.split("/")
    name = parts[-1]

    v1 = k8s_client.CoreV1Api(connector.get_client(cluster))
    v1.patch_node(name, {"spec": {"unschedulable": False}})
    return f"Node {name} uncordoned."


# Registry of executors
_EXECUTORS: dict[str, Any] = {
    "restart_pod": _exec_restart_pod,
    "rollout_restart": _exec_rollout_restart,
    "scale_deployment": _exec_scale_deployment,
    "cordon_node": _exec_cordon_node,
    "drain_node": _exec_drain_node,
    "uncordon_node": _exec_uncordon_node,
}
