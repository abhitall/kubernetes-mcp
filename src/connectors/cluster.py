"""
Kubernetes MCP Server – Multi-Cluster Connector

Provides a unified interface to connect to any Kubernetes flavor using
service-account-based authentication.  Each connector returns a configured
``kubernetes.client.ApiClient`` ready for API calls.

Supported flavors
─────────────────
• Vanilla Kubernetes     – bearer token from SA
• OpenShift / OKD        – SA token or OAuth token
• Rancher (RKE/RKE2)     – SA token or Rancher API token
• GKE                    – SA token (workload identity or static)
• EKS                    – SA token (IRSA or static)
• AKS                    – SA token (workload identity or static)
• K3s / K0s / MicroK8s   – SA token (identical to vanilla)
• Kind / Minikube        – SA token or kubeconfig fallback
• Tanzu (TKG)            – SA token
• DKP                    – SA token
"""

from __future__ import annotations

import base64
import logging
import tempfile
from pathlib import Path

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

from src.models import ClusterConfig, ClusterHealth, K8sFlavor

logger = logging.getLogger(__name__)


class ClusterConnector:
    """Manages connections to multiple Kubernetes clusters."""

    def __init__(self) -> None:
        self._clients: dict[str, k8s_client.ApiClient] = {}
        self._configs: dict[str, ClusterConfig] = {}

    # ── Public API ───────────────────────────────────────────────────────────

    def register(self, cfg: ClusterConfig) -> None:
        """Register a cluster and create an authenticated ApiClient."""
        api_client = self._build_client(cfg)
        self._clients[cfg.name] = api_client
        self._configs[cfg.name] = cfg
        logger.info("Registered cluster '%s' (flavor=%s)", cfg.name, cfg.flavor.value)

    def get_client(self, cluster_name: str) -> k8s_client.ApiClient:
        """Return the ApiClient for a registered cluster."""
        if cluster_name not in self._clients:
            raise KeyError(f"Cluster '{cluster_name}' is not registered")
        return self._clients[cluster_name]

    def get_config(self, cluster_name: str) -> ClusterConfig:
        """Return the ClusterConfig for a registered cluster."""
        if cluster_name not in self._configs:
            raise KeyError(f"Cluster '{cluster_name}' is not registered")
        return self._configs[cluster_name]

    def list_clusters(self) -> list[str]:
        """Return names of all registered clusters."""
        return list(self._clients.keys())

    @property
    def clusters(self) -> dict[str, ClusterConfig]:
        """Return dict of cluster_name → ClusterConfig for all registered clusters."""
        return dict(self._configs)

    def remove(self, cluster_name: str) -> None:
        """De-register a cluster and close its client."""
        api = self._clients.pop(cluster_name, None)
        self._configs.pop(cluster_name, None)
        if api:
            try:
                api.close()
            except Exception:
                pass

    def health_check(self, cluster_name: str) -> ClusterHealth:
        """Perform a lightweight health check against a cluster."""
        try:
            api = self.get_client(cluster_name)
            v1 = k8s_client.CoreV1Api(api)
            version_api = k8s_client.VersionApi(api)

            # Server version
            ver = version_api.get_code()
            server_version = f"{ver.major}.{ver.minor}"

            # Nodes
            nodes = v1.list_node()
            node_count = len(nodes.items)
            ready_nodes = sum(
                1 for n in nodes.items
                for c in (n.status.conditions or [])
                if c.type == "Ready" and c.status == "True"
            )

            # Use a best-effort full listing for a count. This can fail on very
            # large clusters, in which case we fall back to zero.
            pod_count = 0
            try:
                all_pods = v1.list_pod_for_all_namespaces()
                pod_count = len(all_pods.items)
            except Exception:
                pass

            # Warning events (last hour, approximation)
            events = v1.list_event_for_all_namespaces(
                field_selector="type=Warning", limit=100
            )
            warning_events = len(events.items)

            return ClusterHealth(
                cluster_name=cluster_name,
                reachable=True,
                api_server_version=server_version,
                node_count=node_count,
                ready_nodes=ready_nodes,
                pod_count=pod_count,
                warning_events=warning_events,
            )
        except Exception as exc:
            return ClusterHealth(
                cluster_name=cluster_name,
                reachable=False,
                error_message=str(exc),
            )

    # ── Client Builder ───────────────────────────────────────────────────────

    def _build_client(self, cfg: ClusterConfig) -> k8s_client.ApiClient:
        """
        Build an ApiClient using the service-account-based approach.

        Priority:
        0. Proxy URL (kubectl proxy / API gateway – no auth needed)
        1. Explicit SA token (sa_token or sa_token_path)
        2. Flavor-specific auth (OpenShift OAuth, Rancher API token)
        3. Kubeconfig file fallback
        4. In-cluster config (when running inside K8s)
        """

        # ── 0. Proxy / API gateway (e.g. kubectl proxy) ─────────────────────
        if cfg.proxy_url:
            return self._client_from_proxy(cfg)

        # ── 1. Service Account bearer token (universal approach) ─────────────
        token = self._resolve_token(cfg)
        if token:
            return self._client_from_token(cfg, token)

        # ── 2. Flavor-specific fallbacks ─────────────────────────────────────
        if cfg.flavor in (K8sFlavor.OPENSHIFT, K8sFlavor.OKD) and cfg.openshift_oauth_token:
            return self._client_from_token(cfg, cfg.openshift_oauth_token)

        if cfg.flavor == K8sFlavor.RANCHER and cfg.rancher_api_token:
            return self._client_from_token(cfg, cfg.rancher_api_token)

        # ── 3. Kubeconfig fallback ───────────────────────────────────────────
        if cfg.kubeconfig_path:
            return self._client_from_kubeconfig(cfg)

        # ── 4. In-cluster config ─────────────────────────────────────────────
        try:
            k8s_config.load_incluster_config()
            return k8s_client.ApiClient()
        except k8s_config.ConfigException:
            pass

        raise ValueError(
            f"No valid authentication method found for cluster '{cfg.name}'. "
            "Provide proxy_url, sa_token, sa_token_path, kubeconfig_path, or run inside a K8s cluster."
        )

    # ── Token helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_token(cfg: ClusterConfig) -> str | None:
        """Return the SA bearer token from direct value or file path."""
        if cfg.sa_token:
            return cfg.sa_token
        if cfg.sa_token_path:
            path = Path(cfg.sa_token_path)
            if path.exists():
                return path.read_text().strip()
        return None

    @staticmethod
    def _resolve_ca(cfg: ClusterConfig) -> str | None:
        """Return path to CA certificate file, writing a temp file if needed."""
        if cfg.ca_cert_path:
            return cfg.ca_cert_path
        if cfg.ca_cert:
            # Write base64-decoded PEM to a temp file
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".crt")
            tmp.write(base64.b64decode(cfg.ca_cert))
            tmp.close()
            return tmp.name
        return None

    def _client_from_token(self, cfg: ClusterConfig, token: str) -> k8s_client.ApiClient:
        """Create an ApiClient using bearer token auth."""
        configuration = k8s_client.Configuration()
        configuration.host = cfg.api_server
        configuration.api_key = {"authorization": f"Bearer {token}"}
        # Python kubernetes client expects the prefix to NOT be in api_key_prefix
        # when the full "Bearer <token>" is already in api_key value.

        # TLS
        if cfg.skip_tls_verify:
            configuration.verify_ssl = False
        else:
            ca_path = self._resolve_ca(cfg)
            if ca_path:
                configuration.ssl_ca_cert = ca_path
            # If no CA provided and TLS verify is on, the system CAs will be used

        return k8s_client.ApiClient(configuration)

    @staticmethod
    def _client_from_proxy(cfg: ClusterConfig) -> k8s_client.ApiClient:
        """Create an ApiClient that routes through a kubectl proxy or API gateway.

        When kubectl proxy is running (e.g. ``kubectl proxy --port=8001``),
        it creates a local HTTP endpoint that handles all authentication.
        No bearer tokens or TLS configuration is required.
        """
        configuration = k8s_client.Configuration()
        configuration.host = cfg.proxy_url.rstrip("/")
        # kubectl proxy serves plain HTTP and handles auth itself
        configuration.verify_ssl = False
        # No API key needed – the proxy authenticates on our behalf
        return k8s_client.ApiClient(configuration)

    @staticmethod
    def _client_from_kubeconfig(cfg: ClusterConfig) -> k8s_client.ApiClient:
        """Create an ApiClient from a kubeconfig file."""
        return k8s_config.new_client_from_config(
            config_file=cfg.kubeconfig_path,
            context=cfg.kubeconfig_context,
        )


# ── Module-level singleton ───────────────────────────────────────────────────
connector = ClusterConnector()
