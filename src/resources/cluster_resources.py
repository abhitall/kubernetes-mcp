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
    for name, cfg in connector.clusters.items():
        result.append({
            "name": name,
            "flavor": cfg.flavor.value if cfg.flavor else "unknown",
            "api_server": cfg.api_server or "(kubeconfig)",
        })
    return result


def get_cluster_health_resource(cluster: str) -> dict:
    """Return a health snapshot for a cluster."""
    health = connector.health_check(cluster)
    return health.model_dump()
