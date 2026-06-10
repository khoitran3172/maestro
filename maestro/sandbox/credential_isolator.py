"""Environment variable filter for credential isolation."""

from __future__ import annotations

import os

# System environment variables essential for process stability across platforms (especially Windows)
_SYSTEM_ALLOWLIST = {
    "PATH",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "TEMP",
    "TMP",
    "COMSPEC",
    "PATHEXT",
    "WINDIR",
    "APPDATA",
    "LOCALAPPDATA",
    "USERPROFILE",
    "USERNAME",
    "HOME",
    "PWD",
    "LANG",
    "LC_ALL",
}

# Credential variables allowed per specialist
_SPECIALIST_ALLOWLIST = {
    "claude_code": {"ANTHROPIC_API_KEY"},
    "codex": {"OPENAI_API_KEY"},
    "stitch": {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"},
    "grok_build": set(),
    "antigravity": {"GOOGLE_APPLICATION_CREDENTIALS", "GCLOUD_PROJECT", "CLOUDSDK_ACTIVE_CONFIG_NAME"},
}


def get_isolated_env(specialist: str, base_env: dict[str, str]) -> dict[str, str]:
    """Filter environment variables to allow only system variables and specialist credentials."""
    allowed_keys = _SYSTEM_ALLOWLIST.union(_SPECIALIST_ALLOWLIST.get(specialist, set()))
    
    # Also preserve explicit credentials starting with MAESTRO_ for internal settings
    filtered = {}
    for k, v in base_env.items():
        if k in allowed_keys or k.startswith("MAESTRO_") or k.startswith("PYTEST_"):
            filtered[k] = v

    return filtered
