"""
Unit tests for the token-efficient / consolidated operations.

These cover the pure logic (field projection, problem detection, container
status summarization) and the dispatch/aggregation logic (batch_read,
namespace_overview) without requiring a live cluster.
"""

from __future__ import annotations

from types import SimpleNamespace

from src.tools import efficient_ops as eo
from src.tools import k8s_ops


# ── _dig / project_resource ──────────────────────────────────────────────────


def test_dig_simple_path():
    obj = {"status": {"phase": "Running"}}
    assert eo._dig(obj, "status.phase") == "Running"


def test_dig_list_index():
    obj = {"status": {"containerStatuses": [{"restartCount": 3}, {"restartCount": 0}]}}
    assert eo._dig(obj, "status.containerStatuses[0].restartCount") == 3
    assert eo._dig(obj, "status.containerStatuses[1].restartCount") == 0


def test_dig_negative_index():
    obj = {"items": [{"n": 1}, {"n": 2}, {"n": 9}]}
    assert eo._dig(obj, "items[-1].n") == 9


def test_dig_missing_returns_none():
    obj = {"a": {"b": 1}}
    assert eo._dig(obj, "a.c") is None
    assert eo._dig(obj, "a.b.c") is None
    assert eo._dig(obj, "x[0]") is None
    assert eo._dig(obj, "a[0]") is None  # a is a dict, not a list


def test_project_resource_filters_fields(monkeypatch):
    fake = {
        "metadata": {"name": "p1"},
        "status": {"phase": "Running", "podIP": "10.0.0.1"},
        "spec": {"nodeName": "node-a"},
    }
    monkeypatch.setattr(k8s_ops, "get_resource", lambda *a, **k: fake)
    out = eo.project_resource("c", "v1", "Pod", "p1", ["status.phase", "spec.nodeName"])
    assert out == {"status.phase": "Running", "spec.nodeName": "node-a"}


def test_project_resource_passes_through_errors(monkeypatch):
    monkeypatch.setattr(k8s_ops, "get_resource", lambda *a, **k: {"error": "boom"})
    out = eo.project_resource("c", "v1", "Pod", "p1", ["status.phase"])
    assert out == {"error": "boom"}


# ── core-API routing (regression: core kinds must not use CustomObjectsApi) ───


def test_get_resource_core_kind_routes_to_core_api(monkeypatch):
    calls = {}

    def fake_core(cluster, version, plural, *, name=None, namespace=None, query=None):
        calls.update(dict(cluster=cluster, version=version, plural=plural,
                          name=name, namespace=namespace))
        return {"kind": "Pod", "metadata": {"name": name}}

    monkeypatch.setattr(k8s_ops, "_core_api_request", fake_core)
    out = k8s_ops.get_resource("c", "v1", "Pod", "p1", namespace="ns")
    assert out["metadata"]["name"] == "p1"
    assert calls == {"cluster": "c", "version": "v1", "plural": "pods",
                     "name": "p1", "namespace": "ns"}


def test_list_resources_core_kind_routes_to_core_api(monkeypatch):
    def fake_core(cluster, version, plural, *, name=None, namespace=None, query=None):
        assert plural == "pods"
        assert query == {"limit": 2}
        return {"items": [{"metadata": {"name": "a"}}, {"metadata": {"name": "b"}}]}

    monkeypatch.setattr(k8s_ops, "_core_api_request", fake_core)
    out = k8s_ops.list_resources("c", "v1", "Pod", namespace="ns", limit=2)
    assert [p["metadata"]["name"] for p in out] == ["a", "b"]


# ── _pod_is_problem ──────────────────────────────────────────────────────────


def test_pod_is_problem_detection():
    assert eo._pod_is_problem({"phase": "Running", "ready": "1/1", "restarts": 0}) is False
    assert eo._pod_is_problem({"phase": "Succeeded", "ready": "0/0", "restarts": 0}) is False
    assert eo._pod_is_problem({"phase": "Pending", "ready": "0/1", "restarts": 0}) is True
    assert eo._pod_is_problem({"phase": "Running", "ready": "0/1", "restarts": 0}) is True
    assert eo._pod_is_problem({"phase": "Running", "ready": "1/1", "restarts": 5}) is True


# ── _container_status_summary ────────────────────────────────────────────────


def _fake_pod_with_crashloop():
    waiting = SimpleNamespace(reason="CrashLoopBackOff", message="back-off 5m0s")
    state = SimpleNamespace(waiting=waiting, terminated=None, running=None)
    last_term = SimpleNamespace(reason="Error", exit_code=1)
    last_state = SimpleNamespace(terminated=last_term)
    cs = SimpleNamespace(
        name="app", ready=False, restart_count=7, image="app:1.2.3",
        state=state, last_state=last_state,
    )
    status = SimpleNamespace(container_statuses=[cs])
    return SimpleNamespace(status=status)


def test_container_status_summary_crashloop():
    pod = _fake_pod_with_crashloop()
    out = eo._container_status_summary(pod)
    assert len(out) == 1
    c = out[0]
    assert c["name"] == "app"
    assert c["ready"] is False
    assert c["restarts"] == 7
    assert c["state"] == "waiting"
    assert c["reason"] == "CrashLoopBackOff"
    assert c["last_terminated_reason"] == "Error"
    assert c["last_exit_code"] == 1


def test_container_status_summary_empty():
    pod = SimpleNamespace(status=SimpleNamespace(container_statuses=None))
    assert eo._container_status_summary(pod) == []


# ── batch_read ───────────────────────────────────────────────────────────────


def test_batch_read_dispatches_and_injects_cluster(monkeypatch):
    monkeypatch.setitem(eo._BATCH_OPS, "echo", lambda cluster, **kw: {"cluster": cluster, **kw})
    res = eo.batch_read("c1", [{"op": "echo", "args": {"x": 1}}])
    assert res == [{"op": "echo", "ok": True, "result": {"cluster": "c1", "x": 1}}]


def test_batch_read_unknown_op():
    res = eo.batch_read("c1", [{"op": "rm_minus_rf", "args": {}}])
    assert res[0]["ok"] is False
    assert "unknown or non-readonly" in res[0]["error"]


def test_batch_read_op_error_is_isolated(monkeypatch):
    def boom(cluster, **kw):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(eo._BATCH_OPS, "boom", boom)
    monkeypatch.setitem(eo._BATCH_OPS, "ok", lambda cluster, **kw: "fine")
    res = eo.batch_read("c1", [{"op": "boom", "args": {}}, {"op": "ok", "args": {}}])
    assert res[0]["ok"] is False and "kaboom" in res[0]["error"]
    assert res[1] == {"op": "ok", "ok": True, "result": "fine"}


def test_batch_read_only_exposes_readonly_ops():
    # No mutating verbs should be reachable via batch.
    forbidden = {"delete_pod", "scale_deployment", "restart_deployment",
                 "cordon_node", "delete_resource", "create_or_update_resource",
                 "exec_pod", "restart_pod", "uncordon_node"}
    assert forbidden.isdisjoint(eo._BATCH_OPS)


# ── namespace_overview ───────────────────────────────────────────────────────


def test_namespace_overview_aggregates(monkeypatch):
    pods = [
        {"name": "a", "phase": "Running", "ready": "1/1", "restarts": 0},
        {"name": "b", "phase": "Running", "ready": "0/1", "restarts": 3},
        {"name": "c", "phase": "Pending", "ready": "0/1", "restarts": 0},
    ]
    deps = [
        {"name": "web", "replicas": 3, "ready_replicas": 3},
        {"name": "api", "replicas": 2, "ready_replicas": 1},
    ]
    monkeypatch.setattr(k8s_ops, "list_pods", lambda *a, **k: pods)
    monkeypatch.setattr(k8s_ops, "list_deployments", lambda *a, **k: deps)
    monkeypatch.setattr(k8s_ops, "list_pvcs", lambda *a, **k: [
        {"name": "data", "status": "Bound"},
        {"name": "cache", "status": "Pending"},
    ])
    monkeypatch.setattr(k8s_ops, "list_services", lambda *a, **k: [{"name": "svc"}])
    monkeypatch.setattr(eo, "_recent_events", lambda *a, **k: [])

    ov = eo.namespace_overview("c1", "prod")
    assert ov["pods"]["total"] == 3
    assert ov["pods"]["by_phase"] == {"Running": 2, "Pending": 1}
    problem_names = {p["name"] for p in ov["pods"]["problem_pods"]}
    assert problem_names == {"b", "c"}
    assert ov["deployments"]["unhealthy"] == [{"name": "api", "ready": "1/2"}]
    assert ov["unbound_pvcs"] == [{"name": "cache", "status": "Pending"}]
    assert ov["services"] == 1
