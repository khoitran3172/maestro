"""Docker container sandboxing wrapper for specialist executions."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Optional

from maestro.adapters.base import TaskOutput, TaskStatus
from maestro.sandbox.network_policy import NetworkPolicy, get_network_policy


class SandboxContainer:
    """Wraps command execution in a Docker container for security and isolation."""

    def __init__(self, workspace: Path, default_image: str = "python:3.11-alpine"):
        self.workspace = workspace
        self.default_image = default_image

    async def is_docker_available(self) -> bool:
        """Test if the Docker CLI and daemon are accessible."""
        try:
            process = await asyncio.create_subprocess_exec(
                "docker", "info",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await process.communicate()
            return process.returncode == 0
        except Exception:
            return False

    async def run_in_sandbox(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_sec: int = 600,
        network_policy: Optional[NetworkPolicy] = None,
        image: Optional[str] = None,
        input_text: Optional[str] = None,
        force_sandbox: bool = False,
    ) -> TaskOutput:
        """Run a command inside a sandboxed Docker container, with graceful host fallback."""
        sandbox_enabled = os.environ.get("MAESTRO_SANDBOX", "0") == "1" or force_sandbox
        
        if not sandbox_enabled:
            return await self._run_host_subprocess(command, cwd=cwd, env=env, timeout_sec=timeout_sec, input_text=input_text)

        docker_ok = await self.is_docker_available()
        if not docker_ok:
            if force_sandbox:
                return TaskOutput(
                    status=TaskStatus.ERROR,
                    error_message="Docker sandbox is required but Docker daemon is not available.",
                    command=" ".join(command)
                )
            # Graceful fallback to host execution
            print("[WARNING] Docker not available. Falling back to host subprocess execution.")
            return await self._run_host_subprocess(command, cwd=cwd, env=env, timeout_sec=timeout_sec, input_text=input_text)

        # Build Docker command
        target_image = image or self.default_image
        net_policy = network_policy or get_network_policy("generic", command)
        
        docker_args = [
            "run", "--rm",
            "-v", f"{cwd.resolve()}:/workspace",
            "-w", "/workspace",
            "-m", "512m",  # Restrict memory limit
        ]

        # Mount read-only .maestro if it exists
        maestro_dir = cwd / ".maestro"
        if maestro_dir.exists():
            docker_args.extend(["-v", f"{maestro_dir.resolve()}:/workspace/.maestro:ro"])

        # Restrict network
        if net_policy == NetworkPolicy.OFFLINE:
            docker_args.extend(["--network", "none"])

        # Inject environment variables
        for k, v in env.items():
            docker_args.extend(["-e", f"{k}={v}"])

        docker_args.append(target_image)
        docker_args.extend(command)

        # Run via Docker
        start_time = time.monotonic()
        cmd_str = "docker " + " ".join(docker_args)

        try:
            process = await asyncio.create_subprocess_exec(
                "docker", *docker_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE if input_text else None,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(
                        input=input_text.encode() if input_text else None
                    ),
                    timeout=timeout_sec,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                duration = time.monotonic() - start_time
                return TaskOutput(
                    status=TaskStatus.TIMEOUT,
                    duration_sec=duration,
                    estimated_cost_usd=duration * 0.003,
                    error_message=f"Docker container timeout after {timeout_sec}s",
                    command=cmd_str,
                )

            duration = time.monotonic() - start_time
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            # Check if container run failed due to Docker infrastructure issues
            if process.returncode == 125 or "docker: error during connect" in stderr:
                print("[WARNING] Docker run failed. Falling back to host subprocess execution.")
                return await self._run_host_subprocess(command, cwd=cwd, env=env, timeout_sec=timeout_sec, input_text=input_text)

            status = TaskStatus.SUCCESS if process.returncode == 0 else TaskStatus.ERROR
            error_message = "" if process.returncode == 0 else f"Container exited with code {process.returncode}"

            return TaskOutput(
                status=status,
                stdout=stdout,
                stderr=stderr,
                exit_code=process.returncode,
                duration_sec=duration,
                estimated_cost_usd=duration * 0.003,
                error_message=error_message,
                command=cmd_str,
            )

        except Exception as e:
            # Fallback on generic failure
            print(f"[WARNING] Docker initialization failed ({e}). Falling back to host subprocess execution.")
            return await self._run_host_subprocess(command, cwd=cwd, env=env, timeout_sec=timeout_sec, input_text=input_text)

    async def _run_host_subprocess(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_sec: int,
        input_text: Optional[str] = None,
    ) -> TaskOutput:
        """Fallback helper to run command directly on host."""
        start_time = time.monotonic()
        cmd_str = " ".join(command)

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE if input_text else None,
                env={**os.environ, **env},
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(
                        input=input_text.encode() if input_text else None
                    ),
                    timeout=timeout_sec,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                duration = time.monotonic() - start_time
                return TaskOutput(
                    status=TaskStatus.TIMEOUT,
                    duration_sec=duration,
                    estimated_cost_usd=duration * 0.003,
                    error_message=f"Timeout after {timeout_sec}s",
                    command=cmd_str,
                )

            duration = time.monotonic() - start_time
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            status = TaskStatus.SUCCESS if process.returncode == 0 else TaskStatus.ERROR
            error_msg = "" if process.returncode == 0 else f"Non-zero exit code: {process.returncode}"

            return TaskOutput(
                status=status,
                stdout=stdout,
                stderr=stderr,
                exit_code=process.returncode,
                duration_sec=duration,
                estimated_cost_usd=duration * 0.003,
                error_message=error_msg,
                command=cmd_str,
            )

        except Exception as e:
            duration = time.monotonic() - start_time
            return TaskOutput(
                status=TaskStatus.ERROR,
                duration_sec=duration,
                error_message=str(e),
                command=cmd_str,
            )
