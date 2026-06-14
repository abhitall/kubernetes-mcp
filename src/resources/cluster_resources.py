"""
Kubernetes MCP Server – Resources

Expose cluster metadata and health as MCP resources so LLM clients can read
cluster state without invoking tools.
"""

from __future__ import annotations

from src.connectors.cluster import connector


def get_cluster_list() -> list[dict]:
    """Return a summary list of all registered clusters."""
    result = []
    default = connector.default_cluster
    for name, cfg in connector.clusters.items():
        if cfg.proxy_url:
            endpoint = f"proxy:{cfg.proxy_url}"
        elif cfg.api_server:
            endpoint = cfg.api_server
        elif cfg.kubeconfig_yaml:
            endpoint = "(inline kubeconfig)"
        elif cfg.kubeconfig_path:
            endpoint = f"kubeconfig:{cfg.kubeconfig_path}"
        else:
            endpoint = "(in-cluster)"
        result.append({
            "name": name,
            "flavor": cfg.flavor.value if cfg.flavor else "unknown",
            "endpoint": endpoint,
            "namespace": cfg.namespace,
            "is_default": name == default,
        })
    return result


def get_cluster_health_resource(cluster: str) -> dict:
    """Return a health snapshot for a cluster."""
    health = connector.health_check(cluster)
    return health.model_dump()
