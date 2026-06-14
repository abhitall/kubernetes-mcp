"""
Live integration tests for kubeconfig-based cluster connection.

Skipped unless LIVE_KUBECONFIG points at a real, reachable kubeconfig file.

    LIVE_KUBECONFIG=/path/to/kubeconfig.yaml \
        LIVE_KUBECONFIG_CONTEXT=my-cluster-context \
        pytest tests/test_live_kubeconfig.py -v

Optional inline-YAML test:

    LIVE_KUBECONFIG_INLINE=true pytest tests/test_live_kubeconfig.py -v
    (reads the file referenced by LIVE_KUBECONFIG and passes its contents
     to the connector as an inline kubeconfig YAML string)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

KUBECONFIG_PATH = os.getenv("LIVE_KUBECONFIG")
KUBECONFIG_CONTEXT = os.getenv("LIVE_KUBECONFIG_CONTEXT")
INLINE = os.getenv("LIVE_KUBECONFIG_INLINE", "").lower() in {"1", "true", "yes"}
SKIP_TLS = os.getenv("LIVE_KUBECONFIG_SKIP_TLS", "true").lower() in {"1", "true", "yes"}

pytestmark = pytest.mark.skipif(
    not (KUBECONFIG_PATH and Path(KUBECONFIG_PATH).exists()),
    reason="LIVE_KUBECONFIG not set or file not found",
)


@pytest.fixture
def cluster():
    from src.connectors.cluster import ClusterConnector
    from src.models import ClusterConfig

    cn = ClusterConnector()
    if INLINE:
        yaml_text = Path(KUBECONFIG_PATH).read_text()
        cfg = ClusterConfig(
            name="live-kc",
            kubeconfig_yaml=yaml_text,
            kubeconfig_context=KUBECONFIG_CONTEXT,
            skip_tls_verify=SKIP_TLS,
        )
    else:
        cfg = ClusterConfig(
            name="live-kc",
            kubeconfig_path=KUBECONFIG_PATH,
            kubeconfig_context=KUBECONFIG_CONTEXT,
            skip_tls_verify=SKIP_TLS,
        )
    cn.register(cfg)
    yield cn
    cn.remove("live-kc")


def test_health_check(cluster):
    health = cluster.health_check("live-kc")
    assert health.reachable, f"kubeconfig unreachable: {health.error_message}"
    assert health.api_server_version is not None


def test_list_namespaces(cluster):
    from kubernetes import client as k8s_client

    api = cluster.get_client("live-kc")
    v1 = k8s_client.CoreV1Api(api)
    ns = v1.list_namespace()
    assert len(ns.items) >= 1
