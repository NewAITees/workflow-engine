"""
Workspace management using git worktrees.
"""

import logging
import shutil
import subprocess
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

    @property
    def venv_path(self) -> Path:
        """Path to the main workspace's virtual environment."""
        return self.main_work_dir / ".venv"

    def ensure_main_repo(self) -> None:
        """Ensure the main repository exists and dev deps are installed."""
        if not (self.main_work_dir / ".git").exists():
            logger.info(f"Cloning main repository to {self.main_work_dir}")
            result = self.main_git.clone_or_pull()
            if not result.success:
                raise RuntimeError(f"Failed to clone main repo: {result.error}")

        # Prune stale worktree metadata on every startup
        self.main_git.worktree_prune()
        self._ensure_dev_deps()

    def _find_uv(self) -> str:
        """Resolve the uv executable path, falling back to common install locations."""
        found = shutil.which("uv")
        if found:
            return found
        candidates = [
            Path.home() / ".local" / "bin" / "uv",
            Path("/usr/local/bin/uv"),
        ]
        for p in candidates:
            if p.exists():
                return str(p)
        return "uv"  # last resort — subprocess will raise FileNotFoundError clearly

    def _ensure_dev_deps(self) -> None:
        """Install all extras (dev tools: pytest, mypy, ruff) into the main workspace venv.

        Uses UV_PROJECT_ENVIRONMENT so that worktree subprocesses can be pointed
        at this venv via the same env var, avoiding redundant installs per worktree.
        """
        if self.venv_path.exists() and (self.venv_path / "bin" / "pytest").exists():
            return  # Already installed

        uv = self._find_uv()
        logger.info(
            f"Installing dev deps in main workspace venv at {self.venv_path} (uv={uv})"
        )
        try:
            result = subprocess.run(
                [uv, "sync", "--all-extras"],
                cwd=self.main_work_dir,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                logger.warning(
                    f"uv sync --all-extras failed (exit={result.returncode}): "
                    f"{result.stderr[:500]}"
                )
        except Exception as e:
            logger.warning(f"Could not install dev deps: {e}")

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

        # Remove stale worktree registered for the target path
        if worktree_path.exists():
            logger.warning(
                f"Stale worktree directory found at {worktree_path}, removing..."
            )
            self.main_git.worktree_remove(worktree_path)

        # Remove any other worktree that is already holding the target branch
        branch_map = self.main_git.worktree_list_branches()
        if branch in branch_map:
            stale_path = branch_map[branch]
            if stale_path != self.main_work_dir:
                logger.warning(
                    f"Branch '{branch}' already checked out at {stale_path}, removing stale worktree..."
                )
                self.main_git.worktree_remove(stale_path)
                self.main_git.worktree_prune()

        # Fetch once before any branch sync / worktree creation
        fetch_result = self.main_git.fetch_origin()
        if not fetch_result.success:
            raise RuntimeError(f"Failed to fetch origin: {fetch_result.error}")

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
