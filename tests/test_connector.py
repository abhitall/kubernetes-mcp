"""
Unit tests for the cluster connector — kubeconfig (file + inline YAML),
proxy mode (with and without X-Proxy-Token), default-cluster selection,
and the env-var-driven config loader.

These tests do not require a live cluster; they patch the kubernetes
client at the boundary so we can assert on the configured ApiClient
state directly.
"""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest


# ── ClusterConfig schema ────────────────────────────────────────────────────


def test_cluster_config_new_fields_default():
    from src.models import ClusterConfig

    cfg = ClusterConfig(name="x")
    assert cfg.kubeconfig_yaml is None
    assert cfg.proxy_auth_token is None
    assert cfg.proxy_auth_header == "X-Proxy-Token"
    assert cfg.proxy_verify_tls is True
    assert cfg.is_default is False


def test_cluster_config_proxy_with_auth_token():
    from src.models import ClusterConfig

    cfg = ClusterConfig(
        name="nprod-west",
        proxy_url="http://k8s-api-proxy.default.svc.cluster.local:8443",
        proxy_auth_token="shhh",
        is_default=True,
    )
    assert cfg.proxy_auth_token == "shhh"
    assert cfg.is_default is True


# ── Proxy connector ─────────────────────────────────────────────────────────


def test_proxy_connector_http_no_auth():
    from src.connectors.cluster import ClusterConnector
    from src.models import ClusterConfig

    cn = ClusterConnector()
    cfg = ClusterConfig(
        name="local",
        proxy_url="http://localhost:8001/",
    )
    cn.register(cfg)
    api = cn.get_client("local")
    assert api.configuration.host == "http://localhost:8001"
    assert api.configuration.verify_ssl is False
    assert "X-Proxy-Token" not in api.default_headers


def test_proxy_connector_with_auth_token():
    from src.connectors.cluster import ClusterConnector
    from src.models import ClusterConfig

    cn = ClusterConnector()
    cfg = ClusterConfig(
        name="nprod",
        proxy_url="http://k8s-api-proxy.default.svc.cluster.local:8443",
        proxy_auth_token="secret-shared",
    )
    cn.register(cfg)
    api = cn.get_client("nprod")
    assert api.default_headers.get("X-Proxy-Token") == "secret-shared"
    assert "Authorization" not in api.default_headers


def test_proxy_connector_custom_auth_header():
    from src.connectors.cluster import ClusterConnector
    from src.models import ClusterConfig

    cn = ClusterConnector()
    cfg = ClusterConfig(
        name="proxy",
        proxy_url="https://proxy.example.com",
        proxy_auth_token="tok",
        proxy_auth_header="X-Custom-Auth",
    )
    cn.register(cfg)
    api = cn.get_client("proxy")
    assert api.default_headers.get("X-Custom-Auth") == "tok"
    assert "X-Proxy-Token" not in api.default_headers


def test_proxy_connector_https_verify_tls_default():
    from src.connectors.cluster import ClusterConnector
    from src.models import ClusterConfig

    cn = ClusterConnector()
    cn.register(ClusterConfig(name="p", proxy_url="https://proxy.internal.example.com"))
    api = cn.get_client("p")
    assert api.configuration.verify_ssl is True


def test_proxy_connector_https_skip_tls():
    from src.connectors.cluster import ClusterConnector
    from src.models import ClusterConfig

    cn = ClusterConnector()
    cn.register(ClusterConfig(
        name="p",
        proxy_url="https://proxy.internal.example.com",
        proxy_verify_tls=False,
    ))
    api = cn.get_client("p")
    assert api.configuration.verify_ssl is False


# ── Inline kubeconfig YAML ──────────────────────────────────────────────────


_FAKE_KUBECONFIG_YAML = textwrap.dedent(
    """
    apiVersion: v1
    kind: Config
    clusters:
    - name: testc
      cluster:
        server: https://api.example.com:6443
        insecure-skip-tls-verify: true
    users:
    - name: testu
      user:
        token: test-token-abc
    contexts:
    - name: testctx
      context:
        cluster: testc
        user: testu
    current-context: testctx
    """
).strip()


def test_inline_kubeconfig_yaml_loads():
    from src.connectors.cluster import ClusterConnector
    from src.models import ClusterConfig

    cn = ClusterConnector()
    cfg = ClusterConfig(name="inline", kubeconfig_yaml=_FAKE_KUBECONFIG_YAML)
    cn.register(cfg)

    api = cn.get_client("inline")
    assert api.configuration.host == "https://api.example.com:6443"
    # Token from kubeconfig is forwarded as Authorization header
    assert api.default_headers.get("Authorization", "").startswith("Bearer ")


def test_inline_kubeconfig_tempfile_cleanup_on_remove(tmp_path):
    from src.connectors.cluster import ClusterConnector
    from src.models import ClusterConfig

    cn = ClusterConnector()
    cfg = ClusterConfig(name="inline", kubeconfig_yaml=_FAKE_KUBECONFIG_YAML)
    cn.register(cfg)
    owned = list(cn._owned_tmpfiles.get("inline", []))
    assert owned, "tempfile should be tracked for inline kubeconfig"
    assert all(Path(p).exists() for p in owned)

    cn.remove("inline")
    assert all(not Path(p).exists() for p in owned), "tempfile should be cleaned up"


# ── Kubeconfig file ─────────────────────────────────────────────────────────


def test_kubeconfig_path_loads(tmp_path):
    from src.connectors.cluster import ClusterConnector
    from src.models import ClusterConfig

    kc = tmp_path / "kubeconfig.yaml"
    kc.write_text(_FAKE_KUBECONFIG_YAML)

    cn = ClusterConnector()
    cfg = ClusterConfig(name="fromfile", kubeconfig_path=str(kc))
    cn.register(cfg)
    api = cn.get_client("fromfile")
    assert api.configuration.host == "https://api.example.com:6443"


# ── Default-cluster bookkeeping ─────────────────────────────────────────────


def test_default_cluster_first_registered_wins():
    from src.connectors.cluster import ClusterConnector
    from src.models import ClusterConfig

    cn = ClusterConnector()
    cn.register(ClusterConfig(name="a", proxy_url="http://localhost:8001"))
    cn.register(ClusterConfig(name="b", proxy_url="http://localhost:8002"))
    assert cn.default_cluster == "a"
    # get_client() with no arg returns the default
    assert cn.get_client() is cn.get_client("a")


def test_default_cluster_explicit_flag_wins():
    from src.connectors.cluster import ClusterConnector
    from src.models import ClusterConfig

    cn = ClusterConnector()
    cn.register(ClusterConfig(name="a", proxy_url="http://localhost:8001"))
    cn.register(ClusterConfig(name="b", proxy_url="http://localhost:8002", is_default=True))
    assert cn.default_cluster == "b"


def test_set_default_runtime():
    from src.connectors.cluster import ClusterConnector
    from src.models import ClusterConfig

    cn = ClusterConnector()
    cn.register(ClusterConfig(name="a", proxy_url="http://localhost:8001"))
    cn.register(ClusterConfig(name="b", proxy_url="http://localhost:8002"))
    cn.set_default("b")
    assert cn.default_cluster == "b"

    with pytest.raises(KeyError):
        cn.set_default("nope")


def test_remove_updates_default():
    from src.connectors.cluster import ClusterConnector
    from src.models import ClusterConfig

    cn = ClusterConnector()
    cn.register(ClusterConfig(name="a", proxy_url="http://localhost:8001"))
    cn.register(ClusterConfig(name="b", proxy_url="http://localhost:8002"))
    assert cn.default_cluster == "a"
    cn.remove("a")
    assert cn.default_cluster == "b"
    cn.remove("b")
    assert cn.default_cluster is None


# ── Config loader (env var sources) ─────────────────────────────────────────


def _reload_settings():
    """Reload src.config with the current os.environ.

    src.config calls ``load_dotenv()`` at import-time which can re-introduce
    CLUSTER_REGISTRY / KUBECONFIG_PATH from .env. We patch ``dotenv.load_dotenv``
    (the upstream symbol re-imported by ``reload()``) to a no-op so each test's
    environ wins.
    """
    from importlib import reload

    import dotenv

    with patch.object(dotenv, "load_dotenv", lambda *a, **kw: None):
        import src.config
        reload(src.config)
        return src.config.Settings()


def test_settings_cluster_registry_multi():
    registry = json.dumps([
        {"name": "dev", "proxy_url": "http://localhost:8001", "is_default": True},
        {"name": "nprod", "proxy_url": "http://k8s-api-proxy.default.svc.cluster.local:8443"},
    ])
    with patch.dict(os.environ, {"CLUSTER_REGISTRY": registry}, clear=True):
        s = _reload_settings()
    assert [c.name for c in s.clusters] == ["dev", "nprod"]
    assert s.clusters[0].is_default is True


def test_settings_kubeconfig_yaml_env(monkeypatch):
    monkeypatch.setattr(os, "environ", {})
    with patch.dict(os.environ, {
        "KUBECONFIG_YAML": _FAKE_KUBECONFIG_YAML,
        "K8S_CONTEXT": "testctx",
        "K8S_CLUSTER_NAME": "from-env",
    }, clear=True):
        s = _reload_settings()
    assert len(s.clusters) == 1
    c = s.clusters[0]
    assert c.name == "from-env"
    assert c.kubeconfig_yaml == _FAKE_KUBECONFIG_YAML
    assert c.kubeconfig_context == "testctx"
    assert c.is_default is True


def test_settings_kubeconfig_path_env(tmp_path):
    kc = tmp_path / "kc.yaml"
    kc.write_text(_FAKE_KUBECONFIG_YAML)
    with patch.dict(os.environ, {"KUBECONFIG_PATH": str(kc)}, clear=True):
        s = _reload_settings()
    assert len(s.clusters) == 1
    c = s.clusters[0]
    assert c.kubeconfig_path == str(kc)
    assert c.is_default is True


def test_settings_k8s_server_proxy():
    """K8S_SERVER without K8S_TOKEN → proxy mode."""
    with patch.dict(os.environ, {
        "K8S_SERVER": "http://k8s-api-proxy.default.svc.cluster.local:8443",
        "K8S_CLUSTER_NAME": "nprod-via-proxy",
        "PROXY_AUTH_TOKEN": "shh",
    }, clear=True):
        s = _reload_settings()
    assert len(s.clusters) == 1
    c = s.clusters[0]
    assert c.proxy_url == "http://k8s-api-proxy.default.svc.cluster.local:8443"
    assert c.proxy_auth_token == "shh"


def test_settings_k8s_server_apiserver():
    """K8S_SERVER + K8S_TOKEN → direct apiserver mode."""
    with patch.dict(os.environ, {
        "K8S_SERVER": "https://api.cluster.example.com",
        "K8S_TOKEN": "tok",
        "K8S_CA_DATA": "Y2VydA==",
    }, clear=True):
        s = _reload_settings()
    c = s.clusters[0]
    assert c.api_server == "https://api.cluster.example.com"
    assert c.sa_token == "tok"
    assert c.proxy_url is None


def test_settings_default_cluster_env_override():
    registry = json.dumps([
        {"name": "a", "proxy_url": "http://localhost:8001"},
        {"name": "b", "proxy_url": "http://localhost:8002"},
    ])
    with patch.dict(os.environ, {
        "CLUSTER_REGISTRY": registry,
        "DEFAULT_CLUSTER": "b",
    }, clear=True):
        s = _reload_settings()
    by_name = {c.name: c for c in s.clusters}
    assert by_name["b"].is_default is True
    assert by_name["a"].is_default is False


def test_settings_empty_when_nothing_set():
    with patch.dict(os.environ, {}, clear=True):
        s = _reload_settings()
    assert s.clusters == []
