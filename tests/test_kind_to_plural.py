"""Tests for the _kind_to_plural helper function."""

import sys
sys.path.insert(0, '.')

from src.tools.k8s_ops import _kind_to_plural


def test_kind_to_plural_common_resources():
    """Test that common resource kinds are properly pluralized."""
    cases = {
        "Pod": "pods",
        "Service": "services",
        "Deployment": "deployments",
        "Ingress": "ingresses",
        "IngressClass": "ingressclasses",
        "NetworkPolicy": "networkpolicies",
        "StatefulSet": "statefulsets",
        "DaemonSet": "daemonsets",
        "Job": "jobs",
        "CronJob": "cronjobs",
        "PersistentVolumeClaim": "persistentvolumeclaims",
        "HorizontalPodAutoscaler": "horizontalpodautoscalers",
        "Node": "nodes",
        "Namespace": "namespaces",
        "ConfigMap": "configmaps",
        "Secret": "secrets",
        "PodDisruptionBudget": "poddisruptionbudgets",
        "StorageClass": "storageclasses",
        "ClusterRole": "clusterroles",
        "ClusterRoleBinding": "clusterrolebindings",
        "ReplicaSet": "replicasets",
        "Endpoints": "endpoints",
        "ServiceAccount": "serviceaccounts",
        "ResourceQuota": "resourcequotas",
        "LimitRange": "limitranges",
    }
    for kind, expected in cases.items():
        result = _kind_to_plural(kind)
        assert result == expected, f"_kind_to_plural('{kind}') = '{result}', expected '{expected}'"


def test_kind_to_plural_case_insensitive():
    """Test that kind matching is case-insensitive."""
    assert _kind_to_plural("pod") == "pods"
    assert _kind_to_plural("POD") == "pods"
    assert _kind_to_plural("Pod") == "pods"
    assert _kind_to_plural("DEPLOYMENT") == "deployments"


def test_kind_to_plural_fallback_heuristics():
    """Test fallback heuristics for unknown resource types."""
    # Unknown kinds ending in 'y' -> 'ies'
    assert _kind_to_plural("SomePolicy").endswith("ies") or _kind_to_plural("SomePolicy") == "somepolicies"
