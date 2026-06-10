"""Unit tests for Git worktree-based isolation."""

import os
import shutil
import pytest
from pathlib import Path

from maestro.git.branch_manager import GitBranchManager, GitError


@pytest.fixture
def git_workspace(tmp_path):
    """Create a temporary directory representing a git workspace."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # Initialize initial file to prevent empty repository issues
    f = workspace / "main_file.txt"
    f.write_text("initial main file content")
    return workspace


@pytest.mark.asyncio
async def test_init_and_create_worktree(git_workspace):
    """Verify repo initialization and successful worktree creation."""
    manager = GitBranchManager(git_workspace)
    assert not await manager.is_git_repo()

    # Automatically initialized
    worktree_path = await manager.create_isolated_worktree("t1", "codex")
    assert await manager.is_git_repo()
    assert worktree_path.exists()
    assert (worktree_path / "main_file.txt").exists()

    # Check worktree branch exists
    branches = await manager._run_git(["branch"])
    assert "maestro/codex-t1" in branches


@pytest.mark.asyncio
async def test_merge_and_cleanup(git_workspace):
    """Verify modified files in worktree are committed, merged, and cleaned up."""
    manager = GitBranchManager(git_workspace)
    worktree_path = await manager.create_isolated_worktree("t2", "codex")

    # Add a file in the worktree
    new_file = worktree_path / "new_feature.txt"
    new_file.write_text("feature content")

    # Merge and cleanup
    await manager.merge_and_cleanup("t2", "codex")

    # Verify worktree is removed
    assert not worktree_path.exists()

    # Verify file is merged into main workspace
    merged_file = git_workspace / "new_feature.txt"
    assert merged_file.exists()
    assert merged_file.read_text() == "feature content"

    # Verify branch is deleted
    branches = await manager._run_git(["branch"])
    assert "maestro/codex-t2" not in branches


@pytest.mark.asyncio
async def test_merge_conflict_handling(git_workspace):
    """Verify conflict raises GitError, aborts merge, and preserves files safely."""
    manager = GitBranchManager(git_workspace)
    
    # Setup initial git repo and commit
    await manager.init_repo_if_needed()

    # Create worktree
    worktree_path = await manager.create_isolated_worktree("t3", "codex")

    # 1. Modify file in main workspace
    main_file = git_workspace / "main_file.txt"
    main_file.write_text("modified in main")
    await manager._run_git(["add", "main_file.txt"])
    await manager._run_git(["commit", "-m", "conflict update in main"])

    # 2. Modify same file differently in worktree
    worktree_file = worktree_path / "main_file.txt"
    worktree_file.write_text("modified in worktree")

    # Attempt merge - should fail and raise GitError
    with pytest.raises(GitError) as exc_info:
        await manager.merge_and_cleanup("t3", "codex")

    assert "Merge conflict" in str(exc_info.value)

    # Clean up the failed worktree manually for test cleanup
    await manager.cleanup_failed("t3", "codex")
    assert not worktree_path.exists()
