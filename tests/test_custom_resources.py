"""
Unit tests for API discovery + Custom Resource (CRD/CR) support.

Covers the discovery-based plural/scope resolver (which must beat the fragile
heuristic), CRD listing/parsing, api-resource discovery, and resolve-by-Kind
custom-resource access — all without a live cluster.
"""

from __future__ import annotations

from src.tools import k8s_ops


# ── _resolve_resource (discovery beats heuristic) ────────────────────────────


def test_resolve_resource_prefers_discovery(monkeypatch):
    monkeypatch.setattr(k8s_ops, "_discovery_doc", lambda c, av: [
        {"kind": "Gateway", "name": "gateways", "namespaced": True},
        {"kind": "Gateway", "name": "gateways/status", "namespaced": True},  # subresource
    ])
    plural, namespaced = k8s_ops._resolve_resource(
        "c", "gateway.networking.k8s.io/v1", "Gateway")
    assert plural == "gateways"
    assert namespaced is True
    # The heuristic would get this wrong — that's the whole point of discovery:
    assert k8s_ops._kind_to_plural("Gateway") == "gatewaies"


def test_resolve_resource_falls_back_to_heuristic(monkeypatch):
    monkeypatch.setattr(k8s_ops, "_discovery_doc", lambda c, av: [])
    plural, namespaced = k8s_ops._resolve_resource("c", "v1", "Pod")
    assert plural == "pods"
    assert namespaced is None


# ── list_crds ────────────────────────────────────────────────────────────────


class _FakeCustom:
    def list_cluster_custom_object(self, group, version, plural, **kw):
        assert (group, version, plural) == (
            "apiextensions.k8s.io", "v1", "customresourcedefinitions")
        return {"items": [{
            "metadata": {"name": "certificates.cert-manager.io",
                         "creationTimestamp": "2026-01-01T00:00:00Z"},
            "spec": {
                "group": "cert-manager.io",
                "scope": "Namespaced",
                "names": {"kind": "Certificate", "plural": "certificates",
                          "shortNames": ["cert", "certs"]},
                "versions": [
                    {"name": "v1beta1", "served": True, "storage": False},
                    {"name": "v1", "served": True, "storage": True},
                ],
            },
        }]}


def test_list_crds_parsing(monkeypatch):
    monkeypatch.setattr(k8s_ops, "_custom", lambda c: _FakeCustom())
    out = k8s_ops.list_crds("c")
    assert len(out) == 1
    crd = out[0]
    assert crd["kind"] == "Certificate"
    assert crd["group"] == "cert-manager.io"
    assert crd["plural"] == "certificates"
    assert crd["scope"] == "Namespaced"
    assert crd["served_versions"] == ["v1beta1", "v1"]
    assert crd["storage_version"] == "v1"
    assert crd["short_names"] == ["cert", "certs"]


# ── list_api_resources ───────────────────────────────────────────────────────


def test_list_api_resources_flattens_and_skips_subresources(monkeypatch):
    monkeypatch.setattr(k8s_ops, "_all_group_versions",
                        lambda c: ["v1", "cert-manager.io/v1"])
    docs = {
        "v1": [
            {"kind": "Pod", "name": "pods", "namespaced": True,
             "shortNames": ["po"], "verbs": ["get", "list"]},
            {"kind": "Pod", "name": "pods/log", "namespaced": True},  # subresource
        ],
        "cert-manager.io/v1": [
            {"kind": "Certificate", "name": "certificates", "namespaced": True,
             "shortNames": ["cert"], "verbs": ["get"]},
        ],
    }
    monkeypatch.setattr(k8s_ops, "_discovery_doc", lambda c, av: docs.get(av, []))
    out = k8s_ops.list_api_resources("c")
    assert {r["kind"] for r in out} == {"Pod", "Certificate"}
    pod = next(r for r in out if r["kind"] == "Pod")
    assert pod["api_version"] == "v1" and pod["namespaced"] is True


# ── list/get custom resources by Kind ────────────────────────────────────────


def test_list_custom_resources_resolves_version_and_scope(monkeypatch):
    monkeypatch.setattr(k8s_ops, "list_crds", lambda c, *a, **k: [{
        "kind": "Certificate", "group": "cert-manager.io", "scope": "Namespaced",
        "served_versions": ["v1"], "storage_version": "v1",
    }])
    captured = {}

    def fake_list_resources(cluster, api_version, kind, namespace,
                            label_selector, field_selector, limit):
        captured.update(api_version=api_version, kind=kind,
                        namespace=namespace, limit=limit)
        return [{"metadata": {"name": "c1"}}]

    monkeypatch.setattr(k8s_ops, "list_resources", fake_list_resources)
    out = k8s_ops.list_custom_resources("c", "certificate", namespace="default", limit=5)
    assert out == [{"metadata": {"name": "c1"}}]
    assert captured["api_version"] == "cert-manager.io/v1"
    assert captured["kind"] == "Certificate"   # canonical Kind from the CRD
    assert captured["namespace"] == "default"
    assert captured["limit"] == 5


def test_list_custom_resources_cluster_scoped_ignores_namespace(monkeypatch):
    monkeypatch.setattr(k8s_ops, "list_crds", lambda c, *a, **k: [{
        "kind": "ArgusCluster", "group": "platform.argus.io", "scope": "Cluster",
        "served_versions": ["v1"], "storage_version": "v1",
    }])
    captured = {}
    monkeypatch.setattr(k8s_ops, "list_resources",
                        lambda *a, **k: captured.setdefault("args", a) or [])
    k8s_ops.list_custom_resources("c", "ArgusCluster", namespace="argus")
    # positional: (cluster, api_version, kind, namespace, ...) → namespace must be None
    assert captured["args"][3] is None


def test_get_custom_resource_not_found(monkeypatch):
    monkeypatch.setattr(k8s_ops, "list_crds", lambda c, *a, **k: [])
    out = k8s_ops.get_custom_resource("c", "Nope", "x")
    assert "error" in out and "No CRD" in out["error"]
