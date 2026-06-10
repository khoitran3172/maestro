"""Git branch and worktree isolation manager for Maestro tasks."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Optional


class GitError(RuntimeError):
    """Raised when a git command fails."""
    pass


class GitBranchManager:
    """Manages Git branches and worktrees to isolate specialist tasks.

    Allows concurrent executions to run without overriding each other's files.
    """

    _locks_by_loop = {}

    @property
    def lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if loop not in self._locks_by_loop:
            self._locks_by_loop[loop] = asyncio.Lock()
        return self._locks_by_loop[loop]

    def __init__(self, workspace: Path):
        self.workspace = workspace

    async def _run_git(self, args: list[str], cwd: Optional[Path] = None) -> str:
        """Run a git command and return stdout. Raise GitError on failure."""
        cwd = cwd or self.workspace
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await process.communicate()
        if process.returncode != 0:
            err = stderr_bytes.decode("utf-8", errors="replace").strip()
            raise GitError(f"Git command failed: git {' '.join(args)}: {err}")
        return stdout_bytes.decode("utf-8", errors="replace").strip()

    async def is_git_repo(self) -> bool:
        """Check if workspace is a git repository."""
        try:
            await self._run_git(["rev-parse", "--is-inside-work-tree"])
            return True
        except GitError:
            return False

    async def init_repo_if_needed(self) -> None:
        """Initialize git in workspace if not already a repository."""
        if not await self.is_git_repo():
            await self._run_git(["init"])
            await self._run_git(["config", "user.name", "Maestro"])
            await self._run_git(["config", "user.email", "maestro@orchestrator.local"])
            
            # Commit all existing workspace files so they are tracked in worktree checkouts
            try:
                await self._run_git(["add", "."])
                await self._run_git(["commit", "-m", "Initial commit by Maestro"])
            except GitError:
                # If workspace is empty or commit failed, commit a dummy file
                dummy_file = self.workspace / ".maestro_init"
                dummy_file.touch()
                await self._run_git(["add", ".maestro_init"])
                await self._run_git(["commit", "-m", "Initial commit by Maestro"])

    async def get_current_branch(self) -> str:
        """Get the active branch name."""
        await self.init_repo_if_needed()
        try:
            return await self._run_git(["rev-parse", "--abbrev-ref", "HEAD"])
        except GitError:
            return "main"

    async def create_isolated_worktree(self, task_id: str, specialist: str) -> Path:
        """Create a branch and mount an isolated worktree. Returns worktree path."""
        async with self.lock:
            await self.init_repo_if_needed()
            base_branch = await self.get_current_branch()
            branch_name = f"maestro/{specialist}-{task_id}"
            worktree_path = self.workspace / ".maestro" / "worktrees" / task_id

            # Clean up existing worktree path if exists (remnants from crash or canceled runs)
            if worktree_path.exists():
                try:
                    await self._run_git(["worktree", "remove", str(worktree_path), "--force"])
                except Exception:
                    shutil.rmtree(worktree_path, ignore_errors=True)

            # Remove branch if it already exists to start fresh
            try:
                await self._run_git(["show-ref", "--verify", f"refs/heads/{branch_name}"])
                await self._run_git(["branch", "-D", branch_name])
            except GitError:
                pass

            # Create branch from current commit
            await self._run_git(["branch", branch_name, base_branch])

            # Add worktree
            worktree_path.parent.mkdir(parents=True, exist_ok=True)
            await self._run_git(["worktree", "add", str(worktree_path), branch_name])

            return worktree_path

    async def merge_and_cleanup(self, task_id: str, specialist: str) -> None:
        """Commit worktree, merge branch back to base branch, and delete worktree."""
        async with self.lock:
            branch_name = f"maestro/{specialist}-{task_id}"
            worktree_path = self.workspace / ".maestro" / "worktrees" / task_id
            
            if not worktree_path.exists():
                return

            base_branch = await self.get_current_branch()

            # Commit changes in worktree (if any)
            try:
                await self._run_git(["add", "."], cwd=worktree_path)
                status = await self._run_git(["status", "--porcelain"], cwd=worktree_path)
                if status:
                    await self._run_git(["commit", "-m", f"Auto commit by Maestro for task {task_id}"], cwd=worktree_path)
            except GitError:
                pass

            # Merge branch_name into base_branch
            try:
                await self._run_git(["merge", branch_name, "--no-edit"])
            except GitError as e:
                try:
                    await self._run_git(["merge", "--abort"])
                except Exception:
                    pass
                raise GitError(f"Merge conflict while merging branch {branch_name} into {base_branch}: {e}")

            # Cleanup worktree
            try:
                await self._run_git(["worktree", "remove", str(worktree_path), "--force"])
            except Exception:
                shutil.rmtree(worktree_path, ignore_errors=True)

            try:
                await self._run_git(["worktree", "prune"])
            except Exception:
                pass

            # Delete branch
            try:
                await self._run_git(["branch", "-d", branch_name])
            except GitError:
                await self._run_git(["branch", "-D", branch_name])

    async def cleanup_failed(self, task_id: str, specialist: str) -> None:
        """Remove worktree for failed task but preserve branch for debugging."""
        async with self.lock:
            worktree_path = self.workspace / ".maestro" / "worktrees" / task_id
            if not worktree_path.exists():
                return
            
            try:
                await self._run_git(["worktree", "remove", str(worktree_path), "--force"])
            except Exception:
                shutil.rmtree(worktree_path, ignore_errors=True)

            try:
                await self._run_git(["worktree", "prune"])
            except Exception:
                pass
