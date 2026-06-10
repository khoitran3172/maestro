"""Adapter registry — discovers, loads, and manages specialist adapters.

Central place to register and retrieve adapters by name.
Adding a new specialist = create adapter file + register here.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from maestro.adapters.base import SpecialistAdapter
from maestro.adapters.claude_code import ClaudeCodeAdapter
from maestro.adapters.codex import CodexAdapter
from maestro.adapters.stitch import StitchAdapter
from maestro.adapters.grok_build import GrokBuildAdapter
from maestro.adapters.antigravity import AntigravityAdapter


# Default adapter configurations
_DEFAULT_ADAPTERS: dict[str, type[SpecialistAdapter]] = {
    "claude_code": ClaudeCodeAdapter,
    "codex": CodexAdapter,
    "stitch": StitchAdapter,
    "grok_build": GrokBuildAdapter,
    "antigravity": AntigravityAdapter,
}


class AdapterRegistry:
    """Registry for specialist adapters.

    Usage:
        registry = AdapterRegistry()
        registry.register_defaults()
        adapter = registry.get("claude_code")
        await adapter.run(task_input)

    Or with custom config:
        registry = AdapterRegistry()
        registry.register("claude_code", ClaudeCodeAdapter(cli_path="/usr/local/bin/claude"))
    """

    def __init__(self):
        self._adapters: dict[str, SpecialistAdapter] = {}

    def register(self, name: str, adapter: SpecialistAdapter) -> None:
        """Register a specialist adapter instance."""
        self._adapters[name] = adapter

    def register_defaults(
        self,
        *,
        only: Optional[list[str]] = None,
        exclude: Optional[list[str]] = None,
    ) -> None:
        """Register all default adapters (or a subset).

        Args:
            only: If set, only register these adapter names
            exclude: If set, skip these adapter names
        """
        for name, adapter_cls in _DEFAULT_ADAPTERS.items():
            if only and name not in only:
                continue
            if exclude and name in exclude:
                continue
            self._adapters[name] = adapter_cls()

    def get(self, name: str) -> SpecialistAdapter:
        """Get an adapter by name. Raises KeyError if not found."""
        if name not in self._adapters:
            available = ", ".join(sorted(self._adapters.keys()))
            raise KeyError(
                f"Unknown specialist '{name}'. Available: [{available}]"
            )
        return self._adapters[name]

    def has(self, name: str) -> bool:
        """Check if an adapter is registered."""
        return name in self._adapters

    @property
    def available(self) -> list[str]:
        """List of registered adapter names."""
        return sorted(self._adapters.keys())

    async def health_check_all(self) -> dict[str, bool]:
        """Run health checks on all registered adapters.

        Returns dict of {adapter_name: is_healthy}.
        """
        results = {}
        for name, adapter in self._adapters.items():
            try:
                results[name] = await adapter.health_check()
            except Exception:
                results[name] = False
        return results

    async def health_check(self, name: str) -> bool:
        """Run health check on a single adapter."""
        adapter = self.get(name)
        try:
            return await adapter.health_check()
        except Exception:
            return False

    def summary(self) -> dict[str, dict]:
        """Return summary info for all registered adapters."""
        return {
            name: {
                "supports_resume": adapter.supports_resume(),
                "cost_rate_per_sec": adapter.cost_rate_per_sec,
            }
            for name, adapter in self._adapters.items()
        }
