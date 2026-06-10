"""Unit tests for credential isolation and Docker sandboxing."""

import os
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from maestro.sandbox.credential_isolator import get_isolated_env
from maestro.sandbox.container import SandboxContainer
from maestro.sandbox.network_policy import NetworkPolicy, get_network_policy
from maestro.adapters.base import TaskStatus


def test_credential_isolation():
    """Verify environment variable filtering allowlist rules."""
    env = {
        "PATH": "/usr/bin",
        "ANTHROPIC_API_KEY": "sk-ant-123",
        "OPENAI_API_KEY": "sk-proj-456",
        "SECRET_DATABASE_URL": "postgres://user:pass@host",
        "MAESTRO_TEST": "yes",
    }

    # claude_code should keep ANTHROPIC_API_KEY but drop OPENAI_API_KEY and database secrets
    c_env = get_isolated_env("claude_code", env)
    assert "PATH" in c_env
    assert "ANTHROPIC_API_KEY" in c_env
    assert "OPENAI_API_KEY" not in c_env
    assert "SECRET_DATABASE_URL" not in c_env
    assert "MAESTRO_TEST" in c_env

    # codex should keep OPENAI_API_KEY but drop ANTHROPIC_API_KEY
    x_env = get_isolated_env("codex", env)
    assert "PATH" in x_env
    assert "OPENAI_API_KEY" in x_env
    assert "ANTHROPIC_API_KEY" not in x_env


def test_network_policy_resolver():
    """Verify offline vs online routing policies."""
    assert get_network_policy("generic", ["npm", "run", "build"]) == NetworkPolicy.OFFLINE
    assert get_network_policy("generic", ["python", "-m", "pytest"]) == NetworkPolicy.OFFLINE
    assert get_network_policy("claude_code", ["echo", "test"]) == NetworkPolicy.ONLINE


@pytest.mark.asyncio
async def test_sandbox_fallback_when_docker_inactive(tmp_path):
    """Verify sandbox execution falls back to host execution if Docker is inactive."""
    container = SandboxContainer(tmp_path)
    
    with patch.object(container, "is_docker_available", return_value=False):
        # Even with force sandbox, it falls back if not forced to error
        res = await container.run_in_sandbox(
            ["python", "-c", "print('hello fallback')"],
            cwd=tmp_path,
            env={"PATH": os.environ.get("PATH", "")},
            force_sandbox=False
        )
        assert res.status == TaskStatus.SUCCESS
        assert "hello fallback" in res.stdout


@pytest.mark.asyncio
async def test_sandbox_docker_command_construction(tmp_path):
    """Verify correct arguments are constructed for the Docker CLI run invocation."""
    container = SandboxContainer(tmp_path)
    
    # We mock asyncio.create_subprocess_exec to inspect the arguments called
    with patch.object(container, "is_docker_available", return_value=True), \
         patch("asyncio.create_subprocess_exec") as mock_exec:
        
        # Setup mock process communication
        mock_process = AsyncMock()
        mock_process.communicate.return_value = (b"container output", b"")
        mock_process.returncode = 0
        mock_exec.return_value = mock_process

        res = await container.run_in_sandbox(
            ["echo", "hi"],
            cwd=tmp_path,
            env={"MY_VAR": "val"},
            force_sandbox=True,
            network_policy=NetworkPolicy.OFFLINE
        )

        assert res.status == TaskStatus.SUCCESS
        assert res.stdout == "container output"
        
        # Verify docker arguments
        args = mock_exec.call_args[0]
        assert args[0] == "docker"
        assert "run" in args
        assert "--rm" in args
        assert "--network" in args
        assert "none" in args
        assert "-e" in args
        assert "MY_VAR=val" in args
