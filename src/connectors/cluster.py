"""
Kubernetes MCP Server – Multi-Cluster Connector

Provides a unified interface to connect to any Kubernetes flavor using
service-account-based authentication.  Each connector returns a configured
``kubernetes.client.ApiClient`` ready for API calls.

Connection priority (per cluster, first match wins)
───────────────────────────────────────────────────
0. ``proxy_url``                  – kubectl proxy / in-cluster API gateway
1. ``sa_token`` / ``sa_token_path`` – direct API server access via SA bearer token
2. Flavor-specific tokens         – OpenShift OAuth, Rancher API token
3. ``kubeconfig_yaml`` (inline)   – paste a kubeconfig YAML/JSON string
4. ``kubeconfig_path``            – traditional kubeconfig file
5. In-cluster config              – when running inside a Kubernetes pod

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
from urllib.parse import urlparse

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

from src.models import ClusterConfig, ClusterHealth, K8sFlavor

logger = logging.getLogger(__name__)


class ClusterConnector:
    """Manages connections to multiple Kubernetes clusters."""

    def __init__(self) -> None:
        self._clients: dict[str, k8s_client.ApiClient] = {}
        self._configs: dict[str, ClusterConfig] = {}
        # Tempfiles owned by the connector (inline kubeconfig YAML, base64 CA).
        # Removed when the cluster is unregistered.
        self._owned_tmpfiles: dict[str, list[str]] = {}
        self._default_cluster: str | None = None

    # ── Public API ───────────────────────────────────────────────────────────

    def register(self, cfg: ClusterConfig) -> None:
        """Register a cluster and create an authenticated ApiClient."""
        api_client = self._build_client(cfg)
        self._clients[cfg.name] = api_client
        self._configs[cfg.name] = cfg
        if cfg.is_default or self._default_cluster is None:
            self._default_cluster = cfg.name
        logger.info(
            "Registered cluster '%s' (flavor=%s, default=%s)",
            cfg.name,
            cfg.flavor.value,
            self._default_cluster == cfg.name,
        )

    def get_client(self, cluster_name: str | None = None) -> k8s_client.ApiClient:
        """Return the ApiClient for a registered cluster (or the default)."""
        name = cluster_name or self._default_cluster
        if not name:
            raise KeyError("No cluster specified and no default cluster registered")
        if name not in self._clients:
            raise KeyError(f"Cluster '{name}' is not registered")
        return self._clients[name]

    def get_config(self, cluster_name: str | None = None) -> ClusterConfig:
        """Return the ClusterConfig for a registered cluster (or the default)."""
        name = cluster_name or self._default_cluster
        if not name:
            raise KeyError("No cluster specified and no default cluster registered")
        if name not in self._configs:
            raise KeyError(f"Cluster '{name}' is not registered")
        return self._configs[name]

    def list_clusters(self) -> list[str]:
        """Return names of all registered clusters."""
        return list(self._clients.keys())

    @property
    def clusters(self) -> dict[str, ClusterConfig]:
        """Return dict of cluster_name → ClusterConfig for all registered clusters."""
        return dict(self._configs)

    @property
    def default_cluster(self) -> str | None:
        """Name of the cluster used when a tool omits the ``cluster`` argument."""
        return self._default_cluster

    def set_default(self, cluster_name: str) -> None:
        """Mark a registered cluster as the default."""
        if cluster_name not in self._clients:
            raise KeyError(f"Cluster '{cluster_name}' is not registered")
        self._default_cluster = cluster_name

    def remove(self, cluster_name: str) -> None:
        """De-register a cluster, close its client, and clean up tempfiles."""
        api = self._clients.pop(cluster_name, None)
        self._configs.pop(cluster_name, None)
        for tmp in self._owned_tmpfiles.pop(cluster_name, []):
            try:
                Path(tmp).unlink(missing_ok=True)
            except OSError:
                logger.debug("Could not remove tempfile %s", tmp, exc_info=True)
        if api:
            try:
                api.close()
            except Exception:
                pass
        if self._default_cluster == cluster_name:
            self._default_cluster = next(iter(self._clients), None)

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
        Build an ApiClient using the highest-priority auth method available.

        See module docstring for the full priority order.
        """

        # ── 0. Proxy / API gateway (kubectl proxy or k8s-api-proxy) ─────────
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

        # ── 3. Inline kubeconfig YAML ────────────────────────────────────────
        if cfg.kubeconfig_yaml:
            return self._client_from_kubeconfig_yaml(cfg)

        # ── 4. Kubeconfig file fallback ──────────────────────────────────────
        if cfg.kubeconfig_path:
            return self._client_from_kubeconfig(cfg)

        # ── 5. In-cluster config ─────────────────────────────────────────────
        try:
            k8s_config.load_incluster_config()
            logger.info("Cluster '%s' using in-cluster config", cfg.name)
            return k8s_client.ApiClient()
        except k8s_config.ConfigException:
            pass

        raise ValueError(
            f"No valid authentication method found for cluster '{cfg.name}'. "
            "Provide proxy_url, sa_token, sa_token_path, kubeconfig_path, "
            "kubeconfig_yaml, or run inside a Kubernetes cluster."
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

    def _resolve_ca(self, cfg: ClusterConfig) -> str | None:
        """Return path to CA certificate file, writing a temp file if needed."""
        if cfg.ca_cert_path:
            return cfg.ca_cert_path
        if cfg.ca_cert:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".crt")
            tmp.write(base64.b64decode(cfg.ca_cert))
            tmp.close()
            self._owned_tmpfiles.setdefault(cfg.name, []).append(tmp.name)
            return tmp.name
        return None

    def _client_from_token(self, cfg: ClusterConfig, token: str) -> k8s_client.ApiClient:
        """Create an ApiClient using bearer token auth."""
        configuration = k8s_client.Configuration()
        # deepcode ignore Ssrf: intentional connection to admin-configured K8s API server
        configuration.host = cfg.api_server
        configuration.api_key = {"authorization": f"Bearer {token}"}

        # TLS
        if cfg.skip_tls_verify:
            # deepcode ignore TLSVerification: user-configured skip_tls_verify for self-signed K8s certs
            configuration.verify_ssl = False
        else:
            ca_path = self._resolve_ca(cfg)
            if ca_path:
                configuration.ssl_ca_cert = ca_path
            # If no CA provided and TLS verify is on, the system CAs will be used

        api_client = k8s_client.ApiClient(configuration)
        # kubernetes v36+ no longer applies api_key via auth_settings;
        # inject the Authorization header directly.
        api_client.default_headers["Authorization"] = f"Bearer {token}"
        logger.info("Cluster '%s' using bearer-token auth (host=%s)", cfg.name, cfg.api_server)
        return api_client

    @staticmethod
    def _client_from_proxy(cfg: ClusterConfig) -> k8s_client.ApiClient:
        """Create an ApiClient that routes through a kubectl proxy or API gateway.

        ``cfg.proxy_url`` may be plain HTTP (kubectl proxy on localhost) or
        HTTPS (in-cluster k8s-api-proxy behind a service). When the proxy
        enforces a shared secret (e.g. ``PROXY_AUTH_TOKEN``), supply
        ``cfg.proxy_auth_token``; it is forwarded as the
        ``cfg.proxy_auth_header`` (default ``X-Proxy-Token``).
        """
        configuration = k8s_client.Configuration()
        # deepcode ignore Ssrf: intentional connection to admin-configured kubectl proxy
        configuration.host = cfg.proxy_url.rstrip("/")

        scheme = urlparse(cfg.proxy_url).scheme.lower()
        if scheme == "https":
            if not cfg.proxy_verify_tls or cfg.skip_tls_verify:
                # deepcode ignore TLSVerification: user-configured skip_tls_verify for trusted internal proxy
                configuration.verify_ssl = False
            elif cfg.ca_cert_path or cfg.ca_cert:
                # Reuse the same CA helper so a self-signed proxy cert can be trusted.
                # Note: this writes a tempfile but the proxy connector is static, so
                # we accept the leaked tempfile (matches existing _resolve_ca path).
                if cfg.ca_cert_path:
                    configuration.ssl_ca_cert = cfg.ca_cert_path
                elif cfg.ca_cert:
                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".crt")
                    tmp.write(base64.b64decode(cfg.ca_cert))
                    tmp.close()
                    configuration.ssl_ca_cert = tmp.name
        else:
            # Plain HTTP proxy (kubectl proxy / cluster-internal HTTP service)
            # deepcode ignore TLSVerification: kubectl proxy is local HTTP, no TLS needed
            configuration.verify_ssl = False

        api_client = k8s_client.ApiClient(configuration)

        # Optional shared-secret header forwarded to the proxy
        if cfg.proxy_auth_token:
            header = cfg.proxy_auth_header or "X-Proxy-Token"
            api_client.default_headers[header] = cfg.proxy_auth_token

        logger.info(
            "Cluster '%s' using proxy at %s (auth_header=%s)",
            cfg.name,
            cfg.proxy_url,
            "yes" if cfg.proxy_auth_token else "none",
        )
        return api_client

    def _client_from_kubeconfig(self, cfg: ClusterConfig) -> k8s_client.ApiClient:
        """Create an ApiClient from a kubeconfig file."""
        api_client = k8s_config.new_client_from_config(
            config_file=cfg.kubeconfig_path,
            context=cfg.kubeconfig_context,
            persist_config=False,
        )
        self._apply_kubeconfig_overrides(cfg, api_client)
        logger.info(
            "Cluster '%s' using kubeconfig file %s (context=%s)",
            cfg.name,
            cfg.kubeconfig_path,
            cfg.kubeconfig_context or "<current>",
        )
        return api_client

    def _client_from_kubeconfig_yaml(self, cfg: ClusterConfig) -> k8s_client.ApiClient:
        """Create an ApiClient from an inline kubeconfig YAML/JSON string.

        We write the YAML to a tracked tempfile and load via the same path as
        a regular kubeconfig file. This handles every kubeconfig feature
        (multi-context, inline ``certificate-authority-data``, ``exec:`` auth
        providers) without re-implementing client-go logic.
        """
        tmp = tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".yaml", encoding="utf-8"
        )
        tmp.write(cfg.kubeconfig_yaml)
        tmp.close()
        self._owned_tmpfiles.setdefault(cfg.name, []).append(tmp.name)

        api_client = k8s_config.new_client_from_config(
            config_file=tmp.name,
            context=cfg.kubeconfig_context,
            persist_config=False,
        )
        self._apply_kubeconfig_overrides(cfg, api_client)
        logger.info(
            "Cluster '%s' using inline kubeconfig YAML (context=%s)",
            cfg.name,
            cfg.kubeconfig_context or "<current>",
        )
        return api_client

    @staticmethod
    def _apply_kubeconfig_overrides(
        cfg: ClusterConfig, api_client: k8s_client.ApiClient
    ) -> None:
        """Apply cross-cutting overrides after kubeconfig load."""
        # kubernetes v36+ no longer applies api_key via auth_settings;
        # inject the Authorization header directly from the loaded config.
        auth_value = api_client.configuration.api_key.get("authorization", "")
        if auth_value:
            api_client.default_headers["Authorization"] = auth_value
        if cfg.skip_tls_verify:
            # deepcode ignore TLSVerification: user-configured skip_tls_verify for self-signed K8s certs
            api_client.configuration.verify_ssl = False


# ── Module-level singleton ───────────────────────────────────────────────────
connector = ClusterConnector()
