"""Network policy manager for sandboxed execution."""

from __future__ import annotations

from enum import Enum


class NetworkPolicy(str, Enum):
    """Network accessibility configurations for task nodes."""
    ONLINE = "online"
    OFFLINE = "offline"


def get_network_policy(specialist: str, command: list[str]) -> NetworkPolicy:
    """Determine the appropriate network policy based on the task type.

    Deterministic steps (e.g., local tests, builds, linting) default to OFFLINE.
    Frontier model APIs and deployment agents default to ONLINE.
    """
    # Deterministic grading build/test/lint commands run offline
    cmd_str = " ".join(command).lower()
    if "npm run" in cmd_str or "pytest" in cmd_str or "lint" in cmd_str:
        return NetworkPolicy.OFFLINE
    
    # Non-API local helpers can run offline
    if specialist in ("grok_build",):
        return NetworkPolicy.OFFLINE

    return NetworkPolicy.ONLINE
