"""
Kubernetes MCP Server – Application Configuration

Loads settings from environment variables and .env file.

Cluster-registration sources, in priority order
───────────────────────────────────────────────
1. ``CLUSTER_REGISTRY``      – JSON array of full ClusterConfig objects
2. ``KUBECONFIG_YAML``       – inline kubeconfig as a string (single cluster)
3. ``KUBECONFIG_PATH`` / ``KUBECONFIG`` – path to a kubeconfig file
4. ``K8S_SERVER`` + ``K8S_TOKEN`` (+ optional ``K8S_CA_DATA``, ``K8S_CONTEXT``,
   ``K8S_NAMESPACE``, ``K8S_SKIP_TLS_VERIFY``) – minimal env-only auth, e.g.
   for proxy/gateway endpoints.
5. None – server starts empty; clusters registered later via the
   ``register_cluster`` MCP tool or rely on in-cluster config.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from src.models import ClusterConfig, K8sFlavor

# Load .env from project root
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)

logger = logging.getLogger(__name__)


def _truthy(val: str | None) -> bool:
    return (val or "").strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    """Singleton-style settings loaded from environment."""

    def __init__(self) -> None:
        # MCP Server
        self.host: str = os.getenv("MCP_SERVER_HOST", "0.0.0.0")
        self.port: int = int(os.getenv("MCP_SERVER_PORT", "8080"))
        self.transport: str = os.getenv("MCP_TRANSPORT", "streamable-http")
        # Accept legacy LOG_LEVEL for backward compatibility.
        self.log_level: str = os.getenv("MCP_LOG_LEVEL", os.getenv("LOG_LEVEL", "info"))

        # Feature flags
        self.enable_self_heal: bool = _truthy(os.getenv("ENABLE_SELF_HEAL", "true"))
        self.enable_rca: bool = _truthy(os.getenv("ENABLE_RCA", "true"))
        self.read_only: bool = _truthy(os.getenv("READ_ONLY", "false"))
        self.allow_insecure_tls: bool = _truthy(os.getenv("ALLOW_INSECURE_TLS", "false"))

        # Default-cluster override
        self.default_cluster: str | None = os.getenv("DEFAULT_CLUSTER") or None

        # Cluster registry
        self.clusters: list[ClusterConfig] = self._load_clusters()

        # If no entry was marked default and DEFAULT_CLUSTER points at a real
        # cluster, flip the flag.
        if self.default_cluster:
            for c in self.clusters:
                if c.name == self.default_cluster:
                    c.is_default = True

    # ── Cluster loading ──────────────────────────────────────────────────────

    def _load_clusters(self) -> list[ClusterConfig]:
        """Resolve ClusterConfig list from env var sources (see module doc)."""

        # 1. CLUSTER_REGISTRY (full schema, possibly multi-cluster)
        registry = self._parse_registry(os.getenv("CLUSTER_REGISTRY", ""))
        if registry:
            return registry

        # 2. KUBECONFIG_YAML (inline)
        kc_yaml = os.getenv("KUBECONFIG_YAML")
        if kc_yaml:
            return [self._yaml_cluster(kc_yaml)]

        # 3. KUBECONFIG_PATH / KUBECONFIG (file path)
        kc_path = os.getenv("KUBECONFIG_PATH") or os.getenv("KUBECONFIG")
        if kc_path:
            return [self._path_cluster(kc_path)]

        # 4. K8S_SERVER + K8S_TOKEN (minimal env-only auth, proxy or apiserver)
        if os.getenv("K8S_SERVER"):
            return [self._server_cluster()]

        # 5. Empty registry — server starts and clusters can be added later.
        return []

    @staticmethod
    def _parse_registry(raw: str) -> list[ClusterConfig]:
        if not raw or not raw.strip():
            return []
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("CLUSTER_REGISTRY is not valid JSON: %s", exc)
            return []
        if not isinstance(entries, list):
            logger.warning("CLUSTER_REGISTRY must be a JSON array; got %s", type(entries).__name__)
            return []
        configs: list[ClusterConfig] = []
        for i, entry in enumerate(entries):
            try:
                configs.append(ClusterConfig(**entry))
            except Exception as exc:
                logger.warning("CLUSTER_REGISTRY[%d] is malformed: %s", i, exc)
        return configs

    @staticmethod
    def _yaml_cluster(yaml_text: str) -> ClusterConfig:
        ctx = os.getenv("K8S_CONTEXT")
        return ClusterConfig(
            name=os.getenv("K8S_CLUSTER_NAME") or ctx or "default",
            flavor=K8sFlavor(os.getenv("K8S_FLAVOR", "vanilla")),
            namespace=os.getenv("K8S_NAMESPACE", "default"),
            kubeconfig_yaml=yaml_text,
            kubeconfig_context=ctx,
            skip_tls_verify=_truthy(os.getenv("K8S_SKIP_TLS_VERIFY", "false")),
            is_default=True,
        )

    @staticmethod
    def _path_cluster(kc_path: str) -> ClusterConfig:
        ctx = os.getenv("K8S_CONTEXT")
        return ClusterConfig(
            name=os.getenv("K8S_CLUSTER_NAME") or ctx or "default",
            flavor=K8sFlavor(os.getenv("K8S_FLAVOR", "vanilla")),
            namespace=os.getenv("K8S_NAMESPACE", "default"),
            kubeconfig_path=kc_path,
            kubeconfig_context=ctx,
            skip_tls_verify=_truthy(os.getenv("K8S_SKIP_TLS_VERIFY", "false")),
            is_default=True,
        )

    @staticmethod
    def _server_cluster() -> ClusterConfig:
        server = os.environ["K8S_SERVER"]
        token = os.getenv("K8S_TOKEN")
        ca_data = os.getenv("K8S_CA_DATA")
        proxy_token = os.getenv("PROXY_AUTH_TOKEN") or os.getenv("K8S_PROXY_TOKEN")

        # Heuristic: if no token but a non-default port → treat server as a
        # proxy (kubectl proxy / k8s-api-proxy) rather than the apiserver.
        is_proxy = (token is None) or os.getenv("K8S_PROXY", "").lower() in {"1", "true", "yes"}

        if is_proxy:
            return ClusterConfig(
                name=os.getenv("K8S_CLUSTER_NAME", "default"),
                flavor=K8sFlavor(os.getenv("K8S_FLAVOR", "vanilla")),
                namespace=os.getenv("K8S_NAMESPACE", "default"),
                proxy_url=server,
                proxy_auth_token=proxy_token,
                proxy_verify_tls=not _truthy(os.getenv("K8S_SKIP_TLS_VERIFY", "false")),
                skip_tls_verify=_truthy(os.getenv("K8S_SKIP_TLS_VERIFY", "false")),
                is_default=True,
            )

        return ClusterConfig(
            name=os.getenv("K8S_CLUSTER_NAME", "default"),
            flavor=K8sFlavor(os.getenv("K8S_FLAVOR", "vanilla")),
            api_server=server,
            namespace=os.getenv("K8S_NAMESPACE", "default"),
            sa_token=token,
            ca_cert=ca_data,
            skip_tls_verify=_truthy(os.getenv("K8S_SKIP_TLS_VERIFY", "false")),
            is_default=True,
        )


settings = Settings()
