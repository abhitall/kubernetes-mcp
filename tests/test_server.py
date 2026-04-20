"""
Tests for the Kubernetes MCP Server.

These tests validate models, config loading, RCA logic, and self-heal
action mapping without requiring a live cluster.
"""

import json
import os
from unittest.mock import patch

# ── Model Tests ──────────────────────────────────────────────────────────────


def test_k8s_flavor_enum():
    from src.models import K8sFlavor

    assert K8sFlavor.VANILLA.value == "vanilla"
    assert K8sFlavor.OPENSHIFT.value == "openshift"
    assert K8sFlavor.GKE.value == "gke"
    assert K8sFlavor.EKS.value == "eks"
    assert K8sFlavor.AKS.value == "aks"
    assert K8sFlavor.K3S.value == "k3s"
    assert K8sFlavor.RANCHER.value == "rancher"
    assert len(K8sFlavor) == 14


def test_cluster_config_minimal():
    from src.models import ClusterConfig, K8sFlavor

    cfg = ClusterConfig(
        name="test",
        flavor=K8sFlavor.VANILLA,
        api_server="https://localhost:6443",
        sa_token="test-token",
    )
    assert cfg.name == "test"
    assert cfg.flavor == K8sFlavor.VANILLA
    assert cfg.sa_token == "test-token"
    assert cfg.skip_tls_verify is False


def test_cluster_config_full():
    from src.models import ClusterConfig, K8sFlavor

    cfg = ClusterConfig(
        name="prod-gke",
        flavor=K8sFlavor.GKE,
        api_server="https://35.1.2.3",
        sa_token="eyJhbGci...",
        gke_project_id="my-project",
        skip_tls_verify=True,
    )
    assert cfg.gke_project_id == "my-project"
    assert cfg.skip_tls_verify is True


def test_rca_report_defaults():
    from src.models import RCAReport

    report = RCAReport(
        cluster_name="test",
        summary="All good",
        probable_root_cause="None",
        recommended_actions=["Monitor"],
    )
    assert report.conditions == []
    assert report.affected_resources == []
    assert report.timeline == []


def test_heal_action():
    from src.models import HealAction

    action = HealAction(
        action_type="restart_pod",
        target_resource="pod/default/my-pod",
        description="Restart crashing pod",
    )
    assert action.action_type == "restart_pod"
    assert action.parameters == {}


def test_heal_plan():
    from src.models import HealAction, HealPlan

    plan = HealPlan(
        cluster_name="test",
        rca_summary="2 pods crashing",
        actions=[
            HealAction(
                action_type="restart_pod",
                target_resource="pod/default/a",
                description="restart",
            ),
        ],
    )
    assert plan.dry_run is True
    assert plan.requires_approval is True
    assert len(plan.actions) == 1


# ── Config Tests ─────────────────────────────────────────────────────────────


def test_settings_defaults():
    with patch.dict(os.environ, {}, clear=True):
        # Re-import to pick up env changes
        from importlib import reload

        import src.config
        reload(src.config)
        s = src.config.Settings()
        assert s.host == "0.0.0.0"
        assert s.port == 8080
        assert s.transport == "streamable-http"
        assert s.enable_rca is True
        assert s.enable_self_heal is True
        assert s.read_only is False


def test_settings_cluster_registry():
    registry = json.dumps([{
        "name": "test",
        "flavor": "gke",
        "api_server": "https://1.2.3.4",
        "sa_token": "tok",
    }])
    with patch.dict(os.environ, {"CLUSTER_REGISTRY": registry}, clear=True):
        from importlib import reload

        import src.config
        reload(src.config)
        s = src.config.Settings()
        assert len(s.clusters) == 1
        assert s.clusters[0].name == "test"


# ── RCA Logic Tests ─────────────────────────────────────────────────────────


def test_determine_root_cause_empty():
    from src.tools.rca import _determine_root_cause

    cause, recs = _determine_root_cause([])
    assert "No issues" in cause
    assert len(recs) == 1


def test_determine_root_cause_crash_loop():
    from src.models import ConditionDetail
    from src.tools.rca import _determine_root_cause

    conditions = [
        ConditionDetail(
            type="CrashLoopBackOff",
            resource="pod/default/test",
            message="Back-off restarting",
            severity="error",
        ),
    ]
    cause, recs = _determine_root_cause(conditions)
    assert "CrashLoopBackOff" in cause
    assert any("logs" in r.lower() for r in recs)


def test_determine_root_cause_node_not_ready():
    from src.models import ConditionDetail
    from src.tools.rca import _determine_root_cause

    conditions = [
        ConditionDetail(
            type="NodeNotReady",
            resource="node/worker-1",
            message="Node not ready",
            severity="critical",
        ),
    ]
    cause, recs = _determine_root_cause(conditions)
    assert "node" in cause.lower()
    assert any("kubelet" in r.lower() for r in recs)


def test_determine_root_cause_multiple():
    from src.models import ConditionDetail
    from src.tools.rca import _determine_root_cause

    conditions = [
        ConditionDetail(type="NodeNotReady", resource="node/w1", message="Not ready", severity="critical"),
        ConditionDetail(type="CrashLoopBackOff", resource="pod/default/p1", message="Crash", severity="error"),
        ConditionDetail(type="PodPending", resource="pod/default/p2", message="Pending", severity="warning"),
    ]
    cause, recs = _determine_root_cause(conditions)
    assert "node" in cause.lower()
    assert "CrashLoopBackOff" in cause
    assert "Pending" in cause or "pod" in cause.lower()
    assert len(recs) > 3  # Multiple recommendations


# ── Self-Heal Logic Tests ───────────────────────────────────────────────────


def test_actions_for_crash_loop():
    from src.tools.self_heal import _actions_for_condition

    actions = _actions_for_condition("CrashLoopBackOff", "pod/default/test", "default")
    assert len(actions) == 1
    assert actions[0].action_type == "restart_pod"


def test_actions_for_node_not_ready():
    from src.tools.self_heal import _actions_for_condition

    actions = _actions_for_condition("NodeNotReady", "node/worker-1", None)
    assert len(actions) == 2
    types = {a.action_type for a in actions}
    assert "cordon_node" in types
    assert "drain_node" in types


def test_actions_for_image_pull_backoff():
    from src.tools.self_heal import _actions_for_condition

    actions = _actions_for_condition("ImagePullBackOff", "pod/default/test", "default")
    assert len(actions) == 1
    assert actions[0].action_type == "restart_pod"


def test_actions_for_deployment_under_replicated():
    from src.tools.self_heal import _actions_for_condition

    actions = _actions_for_condition("DeploymentUnderReplicated", "deployment/default/web", "default")
    assert len(actions) == 1
    assert actions[0].action_type == "rollout_restart"


def test_actions_for_unknown_event():
    from src.tools.self_heal import _actions_for_condition

    actions = _actions_for_condition("Event:SomeWarning", "pod/default/test", "default")
    assert len(actions) == 0  # Events don't trigger automatic actions


def test_generate_heal_plan():
    from src.models import RCAReport, ConditionDetail
    from src.tools.self_heal import generate_heal_plan

    rca = RCAReport(
        cluster_name="test",
        summary="Issues found",
        conditions=[
            ConditionDetail(type="CrashLoopBackOff", resource="pod/default/p1", message="crash", severity="error"),
            ConditionDetail(type="CrashLoopBackOff", resource="pod/default/p1", message="crash2", severity="error"),
        ],
        probable_root_cause="Pods crashing",
        recommended_actions=["Fix pods"],
    )
    plan = generate_heal_plan(rca)
    # Should deduplicate: same action_type + target_resource
    assert len(plan.actions) == 1
    assert plan.dry_run is True
    assert plan.requires_approval is True


def test_execute_heal_plan_requires_approval():
    from src.models import HealAction, HealPlan
    from src.tools.self_heal import execute_heal_plan

    plan = HealPlan(
        cluster_name="test",
        rca_summary="test",
        actions=[HealAction(action_type="restart_pod", target_resource="pod/default/p1", description="test")],
        requires_approval=True,
    )
    results = execute_heal_plan("test", plan, force=False)
    assert len(results) == 1
    assert results[0].success is False
    assert "approval" in results[0].message.lower()


# ── Prompt Tests ─────────────────────────────────────────────────────────────


def test_get_prompt_valid():
    from src.prompts.k8s_prompts import get_prompt

    prompt = get_prompt("cluster_health_check", cluster="prod")
    assert "prod" in prompt
    assert "health" in prompt.lower()


def test_get_prompt_unknown():
    from src.prompts.k8s_prompts import get_prompt

    result = get_prompt("nonexistent")
    assert "Unknown prompt" in result


def test_get_prompt_missing_var():
    from src.prompts.k8s_prompts import get_prompt

    result = get_prompt("pod_troubleshoot", cluster="prod")  # Missing pod and namespace
    assert "Missing variable" in result
