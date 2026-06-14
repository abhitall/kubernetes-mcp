"""
Live integration tests against the deployed k8s-api-proxy.

These tests are skipped unless a proxy URL is provided. To enable:

    LIVE_PROXY_URL=http://k8s-api-proxy.default.svc.cluster.local:8443 \
        pytest tests/test_live_proxy.py -v

When the MCP server is running outside the cluster, point at the external
endpoint instead:

    LIVE_PROXY_URL=https://mcp.example.com/k8s-api-proxy \
        pytest tests/test_live_proxy.py -v

Optional:

    LIVE_PROXY_TOKEN=<shared-secret>     # X-Proxy-Token header
    LIVE_PROXY_VERIFY_TLS=true|false     # default: true for https, false for http
"""

from __future__ import annotations

import os

import pytest

PROXY_URL = os.getenv("LIVE_PROXY_URL")
PROXY_TOKEN = os.getenv("LIVE_PROXY_TOKEN")
VERIFY_TLS = os.getenv("LIVE_PROXY_VERIFY_TLS", "true").lower() in {"1", "true", "yes"}

pytestmark = pytest.mark.skipif(
    not PROXY_URL,
    reason="LIVE_PROXY_URL not set — skipping live proxy integration test",
)


@pytest.fixture
def cluster():
    """Build a fresh ClusterConnector with a single proxy-backed cluster."""
    from src.connectors.cluster import ClusterConnector
    from src.models import ClusterConfig

    cn = ClusterConnector()
    cn.register(ClusterConfig(
        name="live-proxy",
        proxy_url=PROXY_URL,
        proxy_auth_token=PROXY_TOKEN,
        proxy_verify_tls=VERIFY_TLS,
        skip_tls_verify=not VERIFY_TLS,
    ))
    yield cn
    cn.remove("live-proxy")


def test_health_check_reachable(cluster):
    health = cluster.health_check("live-proxy")
    assert health.reachable, f"proxy unreachable: {health.error_message}"
    assert health.api_server_version is not None
    assert health.node_count >= 0


def test_list_namespaces(cluster):
    from kubernetes import client as k8s_client

    api = cluster.get_client("live-proxy")
    v1 = k8s_client.CoreV1Api(api)
    ns = v1.list_namespace()
    names = [n.metadata.name for n in ns.items]
    assert "kube-system" in names or "default" in names


def test_list_nodes(cluster):
    from kubernetes import client as k8s_client

    api = cluster.get_client("live-proxy")
    v1 = k8s_client.CoreV1Api(api)
    nodes = v1.list_node()
    assert len(nodes.items) >= 1


def test_proxy_auth_token_header_present():
    """Sanity check: the connector wires proxy_auth_token into default_headers."""
    if not PROXY_TOKEN:
        pytest.skip("LIVE_PROXY_TOKEN not set")

    from src.connectors.cluster import ClusterConnector
    from src.models import ClusterConfig

    cn = ClusterConnector()
    cn.register(ClusterConfig(
        name="hdr-check",
        proxy_url=PROXY_URL,
        proxy_auth_token=PROXY_TOKEN,
    ))
    api = cn.get_client("hdr-check")
    assert api.default_headers.get("X-Proxy-Token") == PROXY_TOKEN
    cn.remove("hdr-check")
