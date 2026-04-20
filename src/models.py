"""
Kubernetes MCP Server – Data Models

Pydantic models for cluster configuration, health status, and RCA/self-heal
payloads.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ── Kubernetes Flavor Enumeration ────────────────────────────────────────────


class K8sFlavor(str, Enum):
    """Supported Kubernetes distribution flavors."""

    VANILLA = "vanilla"       # Upstream Kubernetes
    OPENSHIFT = "openshift"   # Red Hat OpenShift (OCP / OKD)
    RANCHER = "rancher"       # SUSE Rancher (RKE / RKE2)
    GKE = "gke"               # Google Kubernetes Engine
    EKS = "eks"               # Amazon Elastic Kubernetes Service
    AKS = "aks"               # Azure Kubernetes Service
    K3S = "k3s"               # Lightweight K3s
    K0S = "k0s"               # Mirantis k0s
    MICROK8S = "microk8s"     # Canonical MicroK8s
    KIND = "kind"             # Kubernetes IN Docker
    MINIKUBE = "minikube"     # Minikube
    TANZU = "tanzu"           # VMware Tanzu (TKG)
    OKD = "okd"               # Community OpenShift
    DKP = "dkp"               # D2iQ Kubernetes Platform


# ── Cluster Configuration ────────────────────────────────────────────────────


class ClusterConfig(BaseModel):
    """Configuration for connecting to a single Kubernetes cluster."""

    name: str = Field(..., description="Unique human-readable cluster identifier")
    flavor: K8sFlavor = Field(K8sFlavor.VANILLA, description="Kubernetes distribution flavor")
    api_server: str = Field("", description="Kubernetes API server URL (https://...)")
    namespace: str = Field("default", description="Default namespace for operations")

    # ── Proxy mode (kubectl proxy / API gateway) ──
    proxy_url: str | None = Field(
        None,
        description=(
            "URL of a kubectl proxy or API gateway (e.g. http://localhost:8001). "
            "When set, all requests are routed through this proxy and no "
            "authentication tokens are needed."
        ),
    )

    # ── Authentication ──
    sa_token: str | None = Field(None, description="Service account bearer token (plain text)")
    sa_token_path: str | None = Field(None, description="Path to file containing the SA token")
    ca_cert: str | None = Field(None, description="CA certificate data (base64-encoded PEM)")
    ca_cert_path: str | None = Field(None, description="Path to CA certificate file")
    kubeconfig_path: str | None = Field(None, description="Path to kubeconfig file (fallback)")
    kubeconfig_context: str | None = Field(None, description="Specific context within kubeconfig")
    skip_tls_verify: bool = Field(False, description="Skip TLS certificate verification (dev only)")

    # ── Flavor-specific ──
    openshift_oauth_token: str | None = Field(None, description="OpenShift OAuth token (if using OAuth)")
    gke_project_id: str | None = Field(None, description="GCP project ID for GKE")
    eks_cluster_name: str | None = Field(None, description="EKS cluster name for AWS auth")
    eks_region: str | None = Field(None, description="AWS region for EKS")
    aks_resource_group: str | None = Field(None, description="Azure resource group for AKS")
    aks_subscription_id: str | None = Field(None, description="Azure subscription ID for AKS")
    rancher_server_url: str | None = Field(None, description="Rancher server URL")
    rancher_api_token: str | None = Field(None, description="Rancher API bearer token")


# ── Cluster Health ───────────────────────────────────────────────────────────


class ClusterHealth(BaseModel):
    """Health/status summary for a connected cluster."""

    cluster_name: str
    reachable: bool
    api_server_version: str | None = None
    node_count: int = 0
    ready_nodes: int = 0
    pod_count: int = 0
    warning_events: int = 0
    error_message: str | None = None


# ── RCA Models ───────────────────────────────────────────────────────────────


class ConditionDetail(BaseModel):
    """A single condition or symptom discovered during RCA."""

    type: str = Field(..., description="Condition type, e.g. 'PodCrashLoop', 'NodeNotReady'")
    resource: str = Field(..., description="Affected resource identifier (e.g. pod/my-app-xyz)")
    namespace: str | None = None
    message: str = ""
    severity: str = Field("warning", description="One of: info, warning, error, critical")
    raw_data: dict[str, Any] = Field(default_factory=dict)


class RCAReport(BaseModel):
    """Root Cause Analysis report for a cluster or workload issue."""

    cluster_name: str
    summary: str
    conditions: list[ConditionDetail] = Field(default_factory=list)
    probable_root_cause: str = ""
    recommended_actions: list[str] = Field(default_factory=list)
    affected_resources: list[str] = Field(default_factory=list)
    timeline: list[dict[str, Any]] = Field(default_factory=list)


# ── Self-Heal Models ─────────────────────────────────────────────────────────


class HealAction(BaseModel):
    """A single remediation action to be executed."""

    action_type: str = Field(..., description="e.g. 'restart_pod', 'scale_deployment', 'cordon_node'")
    target_resource: str = Field(..., description="Resource to act on (e.g. deployment/my-app)")
    namespace: str | None = Field(None, description="Namespace of the target resource")
    parameters: dict[str, Any] = Field(default_factory=dict)
    description: str = ""


class HealResult(BaseModel):
    """Result of executing a self-heal action."""

    action: HealAction
    success: bool
    message: str = ""
    timestamp: str | None = None
    before_state: dict[str, Any] = Field(default_factory=dict)
    after_state: dict[str, Any] = Field(default_factory=dict)


class HealPlan(BaseModel):
    """A plan of remediation actions for cluster issues."""

    cluster_name: str
    rca_summary: str
    actions: list[HealAction] = Field(default_factory=list)
    requires_approval: bool = Field(True, description="Whether human approval is needed")
    dry_run: bool = Field(True, description="If true, actions are simulated only")
    results: list[HealResult] = Field(default_factory=list)
