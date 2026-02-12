"""
Workspace management using git worktrees.
"""

import logging
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from shared.git_operations import GitOperations, GitResult

logger = logging.getLogger(__name__)


class WorkspaceManager:
    """Manages workspaces using git worktrees."""

    def __init__(self, repo: str, main_work_dir: Path):
        self.repo = repo
        self.main_work_dir = main_work_dir
        # The main 'store' operations
        self.main_git = GitOperations(repo, workspace_path=main_work_dir)

    def ensure_main_repo(self) -> None:
        """Ensure the main repository exists."""
        # Check if .git exists to confirm it's a valid repo
        if not (self.main_work_dir / ".git").exists():
            logger.info(f"Cloning main repository to {self.main_work_dir}")
            # clone_or_pull handles both cases, but we want to be sure
            result = self.main_git.clone_or_pull()
            if not result.success:
                raise RuntimeError(f"Failed to clone main repo: {result.error}")
        else:
            # Just ensure it's up to date
            # But we might avoid pulling every time to save time?
            # For now, let's trust clone_or_pull
            pass

    @contextmanager
    def worktree(
        self,
        branch: str,
        agent_id: str,
        *,
        create_branch: bool = False,
        base_branch: str = "main",
    ) -> Generator[GitOperations, None, None]:
        """
        Context manager that provides an isolated worktree.

        Args:
            branch: The branch to checkout in the worktree.
            agent_id: Unique ID for separating worktrees (e.g. 'worker-1').

        Yields:
            A GitOperations instance operating in the new worktree.
        """
        self.ensure_main_repo()

        # Worktree parent directory: {main_work_dir}_worktrees
        # e.g. /path/to/workspaces/NewAITees_workflow-engine_worktrees
        workspace_name = self.main_work_dir.name
        worktree_root = self.main_work_dir.parent / f"{workspace_name}_worktrees"
        worktree_path = worktree_root / agent_id

        logger.info(f"Creating worktree at {worktree_path} on branch {branch}")

        # Clean up existing if stale
        if worktree_path.exists():
            # If directory exists, it might be a left-over worktree or just a dir
            self.main_git.worktree_remove(worktree_path)
            # If remove failed or it wasn't a worktree but a dir, force remove dir?
            # Git worktree remove should handle cleaning the entry.

        def add_wrapped(create_flag: bool) -> GitResult:
            sync_result = self.main_git.ensure_branch_up_to_date(base_branch)
            if not sync_result.success:
                raise RuntimeError(
                    f"Failed to sync base branch '{base_branch}': {sync_result.error}"
                )
            return self.main_git.worktree_add(
                worktree_path,
                branch,
                create_branch=create_flag,
                base_branch=base_branch,
            )

        result = add_wrapped(create_branch)
        if not result.success:
            # Try pruning and retry - sometimes metadata gets stale
            logger.warning(
                f"Worktree add failed: {result.error}. Pruning and retrying..."
            )
            self.main_git.worktree_prune()

            # If the branch is already checked out, we might need to detach or force.
            # But for now, let's assume standard behavior.
            try:
                result = add_wrapped(False)
            except RuntimeError as inner_error:
                raise RuntimeError(
                    f"Failed to recreate worktree: {inner_error}"
                ) from inner_error

            if not result.success:
                raise RuntimeError(f"Failed to create worktree: {result.error}")

        try:
            # Yield a GitOperations instance for this worktree
            yield GitOperations(self.repo, workspace_path=worktree_path)
        finally:
            logger.info(f"Removing worktree at {worktree_path}")
            self.main_git.worktree_remove(worktree_path)
            self.main_git.worktree_prune()
