"""
Kubernetes MCP Server – Application Configuration

Loads settings from environment variables and .env file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

from src.models import ClusterConfig

# Load .env from project root
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)


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
        self.enable_self_heal: bool = os.getenv("ENABLE_SELF_HEAL", "true").lower() == "true"
        self.enable_rca: bool = os.getenv("ENABLE_RCA", "true").lower() == "true"
        self.read_only: bool = os.getenv("READ_ONLY", "false").lower() == "true"

        # Cluster registry
        self.clusters: list[ClusterConfig] = self._load_clusters()

    @staticmethod
    def _load_clusters() -> list[ClusterConfig]:
        """Parse CLUSTER_REGISTRY env var (JSON array) into ClusterConfig list."""
        raw = os.getenv("CLUSTER_REGISTRY", "[]")
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError:
            entries = []
        configs: list[ClusterConfig] = []
        for entry in entries:
            try:
                configs.append(ClusterConfig(**entry))
            except Exception:
                # Skip malformed entries; log later once server is up
                continue
        return configs


settings = Settings()
