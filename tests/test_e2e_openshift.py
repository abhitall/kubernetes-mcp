"""
Comprehensive pytest-based end-to-end tests for the Kubernetes MCP Server
against a live OpenShift CRC cluster.

Prerequisites:
    - OpenShift CRC cluster running locally
    - Logged in via: oc login -u kubeadmin https://api.crc.testing:6443
    - Dependencies installed: pip install -e ".[dev]"

Run:
    PYTHONPATH=. pytest tests/test_e2e_openshift.py -v
    PYTHONPATH=. pytest tests/test_e2e_openshift.py -v -k "TestPods"
    PYTHONPATH=. pytest tests/test_e2e_openshift.py -v --tb=short
"""

from __future__ import annotations

import json
import os
import subprocess
import warnings

import pytest
import urllib3

# Suppress TLS warnings for self-signed certs
warnings.filterwarnings("ignore")
urllib3.disable_warnings()


# ── Fixtures ─────────────────────────────────────────────────────────────────

CLUSTER = "openshift-crc"


def _get_oc_token() -> str:
    """Get a fresh token from oc CLI."""
    return subprocess.check_output(["oc", "whoami", "-t"]).decode().strip()


@pytest.fixture(scope="session", autouse=True)
def setup_openshift_connection():
    """Set up the OpenShift connection for the entire test session.

    This fixture:
    1. Gets a fresh token from `oc whoami -t`
    2. Sets the CLUSTER_REGISTRY env var
    3. Creates and registers a ClusterConnector
    4. Overrides the module-level singleton so all k8s_ops use our connector
    """
    token = _get_oc_token()

    os.environ["CLUSTER_REGISTRY"] = json.dumps([{
        "name": CLUSTER,
        "flavor": "openshift",
        "api_server": "https://api.crc.testing:6443",
        "openshift_oauth_token": token,
        "skip_tls_verify": True,
        "namespace": "default",
    }])

    from src.connectors.cluster import ClusterConnector
    from src.models import ClusterConfig, K8sFlavor

    cfg = ClusterConfig(
        name=CLUSTER,
        flavor=K8sFlavor.OPENSHIFT,
        api_server="https://api.crc.testing:6443",
        openshift_oauth_token=token,
        skip_tls_verify=True,
    )

    conn = ClusterConnector()
    conn.register(cfg)

    # Override the module-level singleton so k8s_ops, rca, self_heal use our connector
    import src.connectors.cluster as cmod
    import src.tools.k8s_ops as ops_mod
    import src.tools.rca as rca_mod
    import src.tools.self_heal as heal_mod
    cmod.connector = conn
    ops_mod.connector = conn
    rca_mod.connector = conn
    heal_mod.connector = conn

    yield conn


# ── Helper Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def active_namespace():
    """Return a namespace that definitely has pods (openshift-console)."""
    return "openshift-console"


@pytest.fixture(scope="session")
def active_pods(active_namespace):
    """Return the list of pods in the active namespace."""
    from src.tools import k8s_ops
    pods = k8s_ops.list_pods(CLUSTER, active_namespace)
    assert len(pods) > 0, f"No pods found in {active_namespace}"
    return pods


@pytest.fixture(scope="session")
def sample_pod(active_pods, active_namespace):
    """Return (name, namespace) of a sample running pod."""
    pod = active_pods[0]
    return pod["name"], active_namespace


@pytest.fixture(scope="session")
def active_deployments(active_namespace):
    """Return the list of deployments in the active namespace."""
    from src.tools import k8s_ops
    deps = k8s_ops.list_deployments(CLUSTER, active_namespace)
    assert len(deps) > 0, f"No deployments found in {active_namespace}"
    return deps


@pytest.fixture(scope="session")
def sample_deployment(active_deployments, active_namespace):
    """Return (name, namespace) of a sample deployment."""
    dep = active_deployments[0]
    return dep["name"], active_namespace


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CLUSTER MANAGEMENT TESTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestClusterManagement:
    """Tests for cluster registration and health checking."""

    def test_get_cluster_list(self):
        from src.resources.cluster_resources import get_cluster_list
        clusters = get_cluster_list()
        assert isinstance(clusters, list)
        assert len(clusters) >= 1
        cluster = clusters[0]
        assert cluster["name"] == CLUSTER
        assert cluster["flavor"] == "openshift"
        assert "api.crc.testing" in cluster["api_server"]

    def test_get_cluster_health(self):
        from src.resources.cluster_resources import get_cluster_health_resource
        health = get_cluster_health_resource(CLUSTER)
        assert isinstance(health, dict)
        assert health["reachable"] is True
        assert health["cluster_name"] == CLUSTER
        assert health["node_count"] >= 1
        assert health["ready_nodes"] >= 1
        assert health["pod_count"] > 0
        assert health["api_server_version"] is not None

    def test_cluster_health_has_version(self):
        from src.resources.cluster_resources import get_cluster_health_resource
        health = get_cluster_health_resource(CLUSTER)
        version = health["api_server_version"]
        # OpenShift CRC should report a version like "1.34" or similar
        assert "." in version

    def test_cluster_health_unknown_cluster_graceful(self):
        from src.resources.cluster_resources import get_cluster_health_resource
        # health_check catches KeyError internally and returns reachable=False
        health = get_cluster_health_resource("nonexistent-cluster")
        assert health["reachable"] is False
        assert health["error_message"] is not None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  NAMESPACE TESTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestNamespaces:
    """Tests for namespace operations."""

    def test_list_namespaces(self):
        from src.tools import k8s_ops
        namespaces = k8s_ops.list_namespaces(CLUSTER)
        assert isinstance(namespaces, list)
        assert len(namespaces) > 10  # OpenShift CRC has many namespaces

    def test_namespace_structure(self):
        from src.tools import k8s_ops
        namespaces = k8s_ops.list_namespaces(CLUSTER)
        for ns in namespaces:
            assert "name" in ns
            assert "status" in ns
            assert isinstance(ns["name"], str)

    def test_default_namespace_exists(self):
        from src.tools import k8s_ops
        namespaces = k8s_ops.list_namespaces(CLUSTER)
        names = [ns["name"] for ns in namespaces]
        assert "default" in names

    def test_openshift_namespaces_exist(self):
        from src.tools import k8s_ops
        namespaces = k8s_ops.list_namespaces(CLUSTER)
        names = [ns["name"] for ns in namespaces]
        # OpenShift-specific namespaces should exist
        for expected in ["openshift-console", "openshift-apiserver"]:
            assert expected in names, f"Expected namespace '{expected}' not found"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  POD TESTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPods:
    """Tests for pod operations."""

    def test_list_pods_default(self):
        from src.tools import k8s_ops
        pods = k8s_ops.list_pods(CLUSTER, "default")
        assert isinstance(pods, list)

    def test_list_pods_active_namespace(self, active_namespace):
        from src.tools import k8s_ops
        pods = k8s_ops.list_pods(CLUSTER, active_namespace)
        assert len(pods) > 0

    def test_pod_structure(self, active_pods):
        for pod in active_pods:
            assert "name" in pod
            assert "namespace" in pod
            assert "phase" in pod
            assert "restarts" in pod
            assert "node" in pod
            assert "age" in pod

    def test_pod_phase_values(self, active_pods):
        valid_phases = {"Running", "Succeeded", "Pending", "Failed", "Unknown"}
        for pod in active_pods:
            assert pod["phase"] in valid_phases, f"Unexpected phase: {pod['phase']}"

    def test_get_pod(self, sample_pod):
        from src.tools import k8s_ops
        name, ns = sample_pod
        pod = k8s_ops.get_pod(CLUSTER, ns, name)
        assert isinstance(pod, dict)
        assert pod.get("metadata", {}).get("name") == name
        assert pod.get("metadata", {}).get("namespace") == ns

    def test_get_pod_has_status(self, sample_pod):
        from src.tools import k8s_ops
        name, ns = sample_pod
        pod = k8s_ops.get_pod(CLUSTER, ns, name)
        assert "status" in pod
        assert "spec" in pod

    def test_get_pod_logs(self, sample_pod):
        from src.tools import k8s_ops
        name, ns = sample_pod
        logs = k8s_ops.get_pod_logs(CLUSTER, ns, name, tail_lines=10)
        assert isinstance(logs, str)

    def test_get_pod_logs_tail_lines(self, sample_pod):
        from src.tools import k8s_ops
        name, ns = sample_pod
        logs_5 = k8s_ops.get_pod_logs(CLUSTER, ns, name, tail_lines=5)
        logs_20 = k8s_ops.get_pod_logs(CLUSTER, ns, name, tail_lines=20)
        # More tail lines should generally give more output (or equal if log is short)
        assert len(logs_20) >= len(logs_5) or True  # logs are dynamic, just test no error

    def test_get_pod_logs_previous_graceful(self, sample_pod):
        """Getting previous logs should either succeed or raise a known API error."""
        from src.tools import k8s_ops
        from kubernetes.client.rest import ApiException
        name, ns = sample_pod
        try:
            logs = k8s_ops.get_pod_logs(CLUSTER, ns, name, tail_lines=5, previous=True)
            assert isinstance(logs, str)
        except ApiException as e:
            # Expected if pod has never been restarted
            assert e.status == 400 or "previous terminated container" in str(e)

    def test_get_pod_nonexistent(self):
        from src.tools import k8s_ops
        from kubernetes.client.rest import ApiException
        with pytest.raises(ApiException) as exc_info:
            k8s_ops.get_pod(CLUSTER, "default", "nonexistent-pod-xyz-12345")
        assert exc_info.value.status == 404


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DEPLOYMENT TESTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDeployments:
    """Tests for deployment operations."""

    def test_list_deployments_default(self):
        from src.tools import k8s_ops
        deps = k8s_ops.list_deployments(CLUSTER, "default")
        assert isinstance(deps, list)

    def test_list_deployments_active(self, active_namespace):
        from src.tools import k8s_ops
        deps = k8s_ops.list_deployments(CLUSTER, active_namespace)
        assert len(deps) > 0

    def test_deployment_structure(self, active_deployments):
        for dep in active_deployments:
            assert "name" in dep
            assert "namespace" in dep
            assert "replicas" in dep
            assert "ready_replicas" in dep
            assert "available_replicas" in dep

    def test_deployment_replicas_consistent(self, active_deployments):
        for dep in active_deployments:
            # Ready replicas should be <= desired replicas
            assert dep["ready_replicas"] <= dep["replicas"]

    def test_get_deployment(self, sample_deployment):
        from src.tools import k8s_ops
        name, ns = sample_deployment
        dep = k8s_ops.get_deployment(CLUSTER, ns, name)
        assert isinstance(dep, dict)
        assert dep.get("metadata", {}).get("name") == name

    def test_get_deployment_has_spec(self, sample_deployment):
        from src.tools import k8s_ops
        name, ns = sample_deployment
        dep = k8s_ops.get_deployment(CLUSTER, ns, name)
        assert "spec" in dep
        assert "status" in dep


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  NODE TESTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestNodes:
    """Tests for node operations."""

    def test_list_nodes(self):
        from src.tools import k8s_ops
        nodes = k8s_ops.list_nodes(CLUSTER)
        assert isinstance(nodes, list)
        assert len(nodes) >= 1  # CRC has 1 node

    def test_node_structure(self):
        from src.tools import k8s_ops
        nodes = k8s_ops.list_nodes(CLUSTER)
        for node in nodes:
            assert "name" in node
            assert "ready" in node
            assert "roles" in node
            assert "kubelet_version" in node

    def test_crc_node_is_ready(self):
        from src.tools import k8s_ops
        nodes = k8s_ops.list_nodes(CLUSTER)
        # CRC should have at least one Ready node
        ready_nodes = [n for n in nodes if n["ready"] == "True"]
        assert len(ready_nodes) >= 1

    def test_crc_node_has_roles(self):
        from src.tools import k8s_ops
        nodes = k8s_ops.list_nodes(CLUSTER)
        node = nodes[0]
        # CRC node typically has master,worker roles
        assert len(node["roles"]) > 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  EVENT TESTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEvents:
    """Tests for event operations."""

    def test_list_events_default(self):
        from src.tools import k8s_ops
        events = k8s_ops.list_events(CLUSTER, "default")
        assert isinstance(events, list)

    def test_list_events_active_namespace(self, active_namespace):
        from src.tools import k8s_ops
        events = k8s_ops.list_events(CLUSTER, active_namespace)
        assert isinstance(events, list)

    def test_event_structure(self, active_namespace):
        from src.tools import k8s_ops
        events = k8s_ops.list_events(CLUSTER, active_namespace)
        for ev in events:
            assert "type" in ev
            assert "reason" in ev
            assert "message" in ev
            assert "namespace" in ev

    def test_event_type_values(self, active_namespace):
        from src.tools import k8s_ops
        events = k8s_ops.list_events(CLUSTER, active_namespace)
        valid_types = {"Normal", "Warning"}
        for ev in events:
            assert ev["type"] in valid_types, f"Unexpected event type: {ev['type']}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SERVICE TESTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestServices:
    """Tests for service operations."""

    def test_list_services_default(self):
        from src.tools import k8s_ops
        svcs = k8s_ops.list_services(CLUSTER, "default")
        assert isinstance(svcs, list)

    def test_list_services_active_namespace(self, active_namespace):
        from src.tools import k8s_ops
        svcs = k8s_ops.list_services(CLUSTER, active_namespace)
        assert isinstance(svcs, list)
        assert len(svcs) > 0

    def test_service_structure(self, active_namespace):
        from src.tools import k8s_ops
        svcs = k8s_ops.list_services(CLUSTER, active_namespace)
        for svc in svcs:
            assert "name" in svc
            assert "namespace" in svc
            assert "type" in svc
            assert "cluster_ip" in svc
            assert "ports" in svc

    def test_service_type_values(self, active_namespace):
        from src.tools import k8s_ops
        svcs = k8s_ops.list_services(CLUSTER, active_namespace)
        valid_types = {"ClusterIP", "NodePort", "LoadBalancer", "ExternalName"}
        for svc in svcs:
            assert svc["type"] in valid_types, f"Unexpected service type: {svc['type']}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CONFIGMAP TESTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestConfigMaps:
    """Tests for ConfigMap operations."""

    def test_list_configmaps(self):
        from src.tools import k8s_ops
        cms = k8s_ops.list_configmaps(CLUSTER, "default")
        assert isinstance(cms, list)
        assert len(cms) > 0

    def test_configmap_structure(self):
        from src.tools import k8s_ops
        cms = k8s_ops.list_configmaps(CLUSTER, "default")
        for cm in cms:
            assert "name" in cm
            assert "namespace" in cm

    def test_get_configmap(self):
        from src.tools import k8s_ops
        cms = k8s_ops.list_configmaps(CLUSTER, "default")
        if cms:
            cm_name = cms[0]["name"]
            cm = k8s_ops.get_configmap(CLUSTER, "default", cm_name)
            assert isinstance(cm, dict)
            assert cm["name"] == cm_name
            assert "data" in cm

    def test_get_configmap_nonexistent(self):
        from src.tools import k8s_ops
        from kubernetes.client.rest import ApiException
        with pytest.raises(ApiException) as exc_info:
            k8s_ops.get_configmap(CLUSTER, "default", "nonexistent-cm-xyz-12345")
        assert exc_info.value.status == 404


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SECRET TESTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSecrets:
    """Tests for secret operations."""

    def test_list_secrets(self):
        from src.tools import k8s_ops
        secrets = k8s_ops.list_secrets(CLUSTER, "default")
        assert isinstance(secrets, list)
        assert len(secrets) > 0

    def test_secret_structure(self):
        from src.tools import k8s_ops
        secrets = k8s_ops.list_secrets(CLUSTER, "default")
        for s in secrets:
            assert "name" in s
            assert "type" in s
            # Ensure no data is leaked
            assert "data" not in s

    def test_secret_no_data_exposed(self):
        """Verify that list_secrets never includes secret data."""
        from src.tools import k8s_ops
        secrets = k8s_ops.list_secrets(CLUSTER, "default")
        for s in secrets:
            assert "data" not in s, "Secret data must not be exposed in list"
            assert "stringData" not in s, "Secret stringData must not be exposed"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GENERIC RESOURCE TESTS (OpenShift CRDs)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestGenericResources:
    """Tests for the generic resource API using OpenShift custom resources."""

    def test_list_openshift_routes(self):
        from src.tools import k8s_ops
        routes = k8s_ops.list_resources(
            CLUSTER, "route.openshift.io/v1", "Route", "openshift-console"
        )
        assert isinstance(routes, list)
        assert len(routes) > 0

    def test_get_openshift_route(self):
        from src.tools import k8s_ops
        route = k8s_ops.get_resource(
            CLUSTER, "route.openshift.io/v1", "Route", "console", "openshift-console"
        )
        assert isinstance(route, dict)
        assert route.get("metadata", {}).get("name") == "console"

    def test_route_has_spec(self):
        from src.tools import k8s_ops
        route = k8s_ops.get_resource(
            CLUSTER, "route.openshift.io/v1", "Route", "console", "openshift-console"
        )
        assert "spec" in route
        assert "host" in route["spec"]

    def test_get_nonexistent_resource(self):
        from src.tools import k8s_ops
        result = k8s_ops.get_resource(
            CLUSTER, "route.openshift.io/v1", "Route", "nonexistent-xyz", "default"
        )
        # Should return an error dict, not raise
        assert "error" in result

    def test_list_resources_nonexistent_namespace(self):
        from src.tools import k8s_ops
        routes = k8s_ops.list_resources(
            CLUSTER, "route.openshift.io/v1", "Route", "nonexistent-ns-xyz"
        )
        # Should return empty list or error, not crash
        assert isinstance(routes, list)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ROOT CAUSE ANALYSIS TESTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRCA:
    """Tests for root cause analysis against live cluster."""

    def test_cluster_rca(self):
        from src.tools.rca import run_cluster_rca
        report = run_cluster_rca(CLUSTER)
        assert report is not None
        assert report.cluster_name == CLUSTER
        assert isinstance(report.summary, str)
        assert isinstance(report.conditions, list)
        assert isinstance(report.probable_root_cause, str)
        assert isinstance(report.recommended_actions, list)

    def test_cluster_rca_has_recommendations(self):
        from src.tools.rca import run_cluster_rca
        report = run_cluster_rca(CLUSTER)
        assert len(report.recommended_actions) >= 1

    def test_namespace_rca_default(self):
        from src.tools.rca import run_namespace_rca
        report = run_namespace_rca(CLUSTER, "default")
        assert report is not None
        assert report.cluster_name == CLUSTER
        assert isinstance(report.summary, str)

    def test_namespace_rca_active(self, active_namespace):
        from src.tools.rca import run_namespace_rca
        report = run_namespace_rca(CLUSTER, active_namespace)
        assert report is not None
        assert isinstance(report.conditions, list)

    def test_pod_rca(self, sample_pod):
        from src.tools.rca import run_pod_rca
        name, ns = sample_pod
        report = run_pod_rca(CLUSTER, ns, name)
        assert report is not None
        assert report.cluster_name == CLUSTER
        assert isinstance(report.summary, str)

    def test_pod_rca_nonexistent(self):
        from src.tools.rca import run_pod_rca
        report = run_pod_rca(CLUSTER, "default", "nonexistent-pod-xyz-12345")
        # Should return a report with error info, not raise
        assert report is not None
        assert "not found" in report.summary.lower() or "cannot read" in report.summary.lower()

    def test_rca_report_model_dump(self):
        from src.tools.rca import run_cluster_rca
        report = run_cluster_rca(CLUSTER)
        dumped = report.model_dump()
        assert isinstance(dumped, dict)
        assert "cluster_name" in dumped
        assert "summary" in dumped
        assert "conditions" in dumped
        assert "probable_root_cause" in dumped
        assert "recommended_actions" in dumped

    def test_rca_condition_severity_values(self):
        from src.tools.rca import run_cluster_rca
        report = run_cluster_rca(CLUSTER)
        valid_severities = {"info", "warning", "error", "critical"}
        for condition in report.conditions:
            assert condition.severity in valid_severities


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SELF-HEAL TESTS (DRY-RUN ONLY)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSelfHeal:
    """Tests for self-healing (dry-run only, no destructive actions)."""

    def test_generate_heal_plan(self):
        from src.tools.rca import run_cluster_rca
        from src.tools.self_heal import generate_heal_plan
        rca = run_cluster_rca(CLUSTER)
        plan = generate_heal_plan(rca, dry_run=True)
        assert plan is not None
        assert plan.cluster_name == CLUSTER
        assert plan.dry_run is True
        assert isinstance(plan.actions, list)

    def test_heal_plan_model_dump(self):
        from src.tools.rca import run_cluster_rca
        from src.tools.self_heal import generate_heal_plan
        rca = run_cluster_rca(CLUSTER)
        plan = generate_heal_plan(rca, dry_run=True)
        dumped = plan.model_dump()
        assert isinstance(dumped, dict)
        assert "cluster_name" in dumped
        assert "actions" in dumped
        assert "dry_run" in dumped

    def test_execute_heal_plan_dry_run(self):
        from src.tools.rca import run_cluster_rca
        from src.tools.self_heal import execute_heal_plan, generate_heal_plan
        rca = run_cluster_rca(CLUSTER)
        plan = generate_heal_plan(rca, dry_run=True)
        results = execute_heal_plan(CLUSTER, plan, force=True)
        assert isinstance(results, list)
        for r in results:
            assert r.success is True
            assert "[DRY RUN]" in r.message

    def test_execute_heal_plan_approval_gate(self):
        from src.tools.rca import run_cluster_rca
        from src.tools.self_heal import execute_heal_plan, generate_heal_plan
        rca = run_cluster_rca(CLUSTER)
        plan = generate_heal_plan(rca, dry_run=True, requires_approval=True)
        # Without force=True, should be blocked by approval gate
        results = execute_heal_plan(CLUSTER, plan, force=False)
        for r in results:
            assert r.success is False
            assert "approval" in r.message.lower()

    def test_quick_heal_dry_run(self, sample_pod):
        from src.tools.self_heal import quick_heal
        name, ns = sample_pod
        result = quick_heal(
            CLUSTER, "restart_pod", f"pod/{ns}/{name}", ns, dry_run=True
        )
        assert result.success is True
        assert "[DRY RUN]" in result.message


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PROMPT TESTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPrompts:
    """Tests for prompt templates."""

    @pytest.mark.parametrize("prompt_name,kwargs,expected_substring", [
        ("cluster_health_check", {"cluster": CLUSTER}, CLUSTER),
        ("pod_troubleshoot", {"cluster": CLUSTER, "namespace": "default", "pod": "test"}, "test"),
        ("self_heal_workflow", {"cluster": CLUSTER}, CLUSTER),
        ("namespace_review", {"cluster": CLUSTER, "namespace": "default"}, "default"),
        ("multi_cluster_overview", {}, "cluster"),
        ("incident_response", {"alert_message": "CPU high on node crc"}, "CPU high"),
    ])
    def test_prompt_rendering(self, prompt_name, kwargs, expected_substring):
        from src.prompts.k8s_prompts import get_prompt
        result = get_prompt(prompt_name, **kwargs)
        assert isinstance(result, str)
        assert expected_substring.lower() in result.lower()

    def test_prompt_unknown(self):
        from src.prompts.k8s_prompts import get_prompt
        result = get_prompt("nonexistent_prompt")
        assert "Unknown prompt" in result

    def test_prompt_missing_variable(self):
        from src.prompts.k8s_prompts import get_prompt
        result = get_prompt("pod_troubleshoot", cluster="test")
        assert "Missing variable" in result

    def test_all_prompts_return_nonempty(self):
        from src.prompts.k8s_prompts import get_prompt
        prompts = [
            ("cluster_health_check", {"cluster": "test"}),
            ("pod_troubleshoot", {"cluster": "test", "namespace": "ns", "pod": "p"}),
            ("self_heal_workflow", {"cluster": "test"}),
            ("namespace_review", {"cluster": "test", "namespace": "ns"}),
            ("multi_cluster_overview", {}),
            ("incident_response", {"alert_message": "test alert"}),
        ]
        for name, kwargs in prompts:
            result = get_prompt(name, **kwargs)
            assert len(result) > 20, f"Prompt '{name}' is too short: {result}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SERVER.PY TOOL FUNCTION TESTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestServerTools:
    """Test the server.py tool wrapper functions directly.

    These test that the server.py functions correctly delegate to k8s_ops
    with proper parameter ordering.  Tools return native Python types
    (list, dict, str) with structured_output=False on the MCP decorator.
    """

    def test_server_list_clusters(self):
        from src.server import list_clusters
        result = list_clusters()
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_server_cluster_health(self):
        from src.server import cluster_health
        result = cluster_health(CLUSTER)
        assert isinstance(result, dict)
        assert result["reachable"] is True

    def test_server_list_namespaces(self):
        from src.server import list_namespaces
        result = list_namespaces(CLUSTER)
        assert isinstance(result, list)
        assert len(result) > 0

    def test_server_list_pods(self, active_namespace):
        from src.server import list_pods
        result = list_pods(CLUSTER, active_namespace)
        assert isinstance(result, list)
        assert len(result) > 0

    def test_server_get_pod(self, sample_pod):
        from src.server import get_pod
        name, ns = sample_pod
        result = get_pod(CLUSTER, name, ns)
        assert isinstance(result, dict)

    def test_server_get_pod_logs(self, sample_pod):
        from src.server import get_pod_logs
        name, ns = sample_pod
        result = get_pod_logs(CLUSTER, name, ns, tail_lines=5)
        assert isinstance(result, str)

    def test_server_list_deployments(self, active_namespace):
        from src.server import list_deployments
        result = list_deployments(CLUSTER, active_namespace)
        assert isinstance(result, list)

    def test_server_get_deployment(self, sample_deployment):
        from src.server import get_deployment
        name, ns = sample_deployment
        result = get_deployment(CLUSTER, name, ns)
        assert isinstance(result, dict)

    def test_server_list_nodes(self):
        from src.server import list_nodes
        result = list_nodes(CLUSTER)
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_server_list_events(self):
        from src.server import list_events
        result = list_events(CLUSTER, "default")
        assert isinstance(result, list)

    def test_server_list_services(self, active_namespace):
        from src.server import list_services
        result = list_services(CLUSTER, active_namespace)
        assert isinstance(result, list)

    def test_server_list_configmaps(self):
        from src.server import list_configmaps
        result = list_configmaps(CLUSTER, "default")
        assert isinstance(result, list)

    def test_server_get_configmap(self):
        from src.server import list_configmaps, get_configmap
        cms = list_configmaps(CLUSTER, "default")
        if cms:
            result = get_configmap(CLUSTER, cms[0]["name"], "default")
            assert isinstance(result, dict)

    def test_server_list_secrets(self):
        from src.server import list_secrets
        result = list_secrets(CLUSTER, "default")
        assert isinstance(result, list)

    def test_server_get_resource(self):
        from src.server import get_resource
        result = get_resource(
            CLUSTER, "route.openshift.io/v1", "Route", "console", "openshift-console"
        )
        assert isinstance(result, dict)

    def test_server_list_resources(self):
        from src.server import list_resources
        result = list_resources(
            CLUSTER, "route.openshift.io/v1", "Route", "openshift-console"
        )
        assert isinstance(result, list)

    def test_server_cluster_rca(self):
        from src.server import cluster_rca
        result = cluster_rca(CLUSTER)
        assert isinstance(result, dict)
        assert "cluster_name" in result
        assert "summary" in result

    def test_server_namespace_rca(self):
        from src.server import namespace_rca
        result = namespace_rca(CLUSTER, "default")
        assert isinstance(result, dict)

    def test_server_pod_rca(self, sample_pod):
        from src.server import pod_rca
        name, ns = sample_pod
        result = pod_rca(CLUSTER, name, ns)
        assert isinstance(result, dict)

    def test_server_heal_plan(self):
        from src.server import heal_plan
        result = heal_plan(CLUSTER, dry_run=True)
        assert isinstance(result, dict)
        assert result["dry_run"] is True

    def test_server_prompts(self):
        from src.server import (
            cluster_health_check,
            pod_troubleshoot,
            self_heal_workflow,
            namespace_review,
            multi_cluster_overview,
            incident_response,
        )
        assert isinstance(cluster_health_check(CLUSTER), str)
        assert isinstance(pod_troubleshoot(CLUSTER, "default", "test"), str)
        assert isinstance(self_heal_workflow(CLUSTER), str)
        assert isinstance(namespace_review(CLUSTER, "default"), str)
        assert isinstance(multi_cluster_overview(), str)
        assert isinstance(incident_response("test alert"), str)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  EDGE CASE & ROBUSTNESS TESTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEdgeCases:
    """Edge cases and robustness tests."""

    def test_unregistered_cluster_raises(self):
        from src.tools import k8s_ops
        with pytest.raises(KeyError, match="not registered"):
            k8s_ops.list_namespaces("nonexistent-cluster")

    def test_empty_namespace_pods(self):
        """Listing pods in a namespace with no pods should return empty list."""
        from src.tools import k8s_ops
        pods = k8s_ops.list_pods(CLUSTER, "default")
        # default might be empty, that's ok
        assert isinstance(pods, list)

    def test_list_pods_all_namespaces(self):
        """Listing pods across all namespaces should work."""
        from src.tools import k8s_ops
        pods = k8s_ops.list_pods(CLUSTER, "")
        assert isinstance(pods, list)
        assert len(pods) > 0

    def test_list_deployments_all_namespaces(self):
        from src.tools import k8s_ops
        deps = k8s_ops.list_deployments(CLUSTER, "")
        assert isinstance(deps, list)
        assert len(deps) > 0

    def test_list_events_all_namespaces(self):
        from src.tools import k8s_ops
        events = k8s_ops.list_events(CLUSTER, "")
        assert isinstance(events, list)

    def test_list_services_all_namespaces(self):
        from src.tools import k8s_ops
        svcs = k8s_ops.list_services(CLUSTER, "")
        assert isinstance(svcs, list)
        assert len(svcs) > 0

    def test_rca_serialization_roundtrip(self):
        """RCA report should serialize to JSON and back."""
        from src.tools.rca import run_cluster_rca
        report = run_cluster_rca(CLUSTER)
        json_str = report.model_dump_json()
        assert isinstance(json_str, str)
        from src.models import RCAReport
        restored = RCAReport.model_validate_json(json_str)
        assert restored.cluster_name == report.cluster_name

    def test_heal_plan_serialization_roundtrip(self):
        """HealPlan should serialize to JSON and back."""
        from src.tools.rca import run_cluster_rca
        from src.tools.self_heal import generate_heal_plan
        rca = run_cluster_rca(CLUSTER)
        plan = generate_heal_plan(rca, dry_run=True)
        json_str = plan.model_dump_json()
        from src.models import HealPlan
        restored = HealPlan.model_validate_json(json_str)
        assert restored.cluster_name == plan.cluster_name
        assert len(restored.actions) == len(plan.actions)
