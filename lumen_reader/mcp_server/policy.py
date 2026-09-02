"""Central public-surface and output-budget policy."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ServerPolicy:
    max_resource_bytes: int = 65_536
    max_connections: int = 8
    enable_prompts: bool = True
    enable_admin_tools: bool = False
    include_paths: bool = True


DEFAULT_POLICY = ServerPolicy()
