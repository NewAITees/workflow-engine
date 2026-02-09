"""Git operations for worker agent with per-issue worktree support."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generic, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class GitResult(Generic[T]):
    """Result of a git operation."""

    success: bool
    returncode: int
    stdout: str
    stderr: str
    command: list[str] = field(default_factory=list)
    value: T | None = None

    @property
    def output(self) -> str:
        """Compatibility alias used by existing code."""
        return self.stdout

    @property
    def error(self) -> str | None:
        """Compatibility alias used by existing code."""
        if self.success:
            return None
        return self.stderr or f"Exit code: {self.returncode}"


class GitOperations:
    """Git operations for implementing features."""

    def __init__(
        self,
        repo: str,
        work_base: Path | None = None,
        workspace_path: Path | None = None,
    ):
        self.repo = repo

        if workspace_path:
            self.workspace = Path(workspace_path)
            self.work_base = self.workspace.parent
        elif work_base:
            self.work_base = Path(work_base)
            self.work_base.mkdir(parents=True, exist_ok=True)
            self.workspace = self.work_base / repo.replace("/", "_")
        else:
            raise ValueError("Either work_base or workspace_path must be provided")

    def _run(
        self,
        args: list[str],
        cwd: Path | None = None,
        check: bool = True,
    ) -> GitResult[None]:
        """Run a git command."""
        cmd = ["git"] + args
        work_dir = cwd or self.workspace

        logger.debug(f"Running: {' '.join(cmd)} in {work_dir}")

        try:
            result = subprocess.run(
                cmd,
                cwd=work_dir,
                capture_output=True,
                text=True,
                check=check,
            )
            success = result.returncode == 0
            return GitResult(
                success=success,
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                command=cmd,
            )
        except subprocess.CalledProcessError as e:
            return GitResult(
                success=False,
                returncode=e.returncode,
                stdout=e.stdout or "",
                stderr=e.stderr or "",
                command=cmd,
            )

    def clone_or_pull(self) -> GitResult[None]:
        """Clone the repository or pull if it exists."""
        if self.workspace.exists():
            logger.info(f"Updating existing workspace: {self.workspace}")
            default_branch = self.get_default_branch()
            self._run(["fetch", "origin"], check=False)
            self._run(["checkout", default_branch], check=False)
            self._run(["reset", "--hard", f"origin/{default_branch}"])
            self._run(["clean", "-fd"])
            return self._run(["pull"])

        logger.info(f"Cloning repository to: {self.workspace}")
        return self._run(
            ["clone", f"https://github.com/{self.repo}.git", str(self.workspace)],
            cwd=self.work_base,
        )

    def get_default_branch(self) -> str:
        """Get the default branch name (main or master)."""
        result = self._run(["symbolic-ref", "refs/remotes/origin/HEAD"], check=False)
        if result.success:
            return result.stdout.strip().split("/")[-1]
        return "main"

    def create_branch(self, branch_name: str) -> GitResult[None]:
        """Create and checkout a new branch."""
        default_branch = self.get_default_branch()
        self._run(["checkout", default_branch], check=False)
        self._run(["pull"])
        self._run(["branch", "-D", branch_name], check=False)
        return self._run(["checkout", "-b", branch_name])

    def checkout_branch_from_remote(self, branch_name: str) -> GitResult[None]:
        """Checkout a local branch from origin/<branch_name> if it exists."""
        self._run(["fetch", "origin"], check=False)
        remote_ref = f"origin/{branch_name}"
        remote_exists = self._run(["rev-parse", "--verify", remote_ref], check=False)
        if remote_exists.success:
            return self._run(["checkout", "-B", branch_name, remote_ref])
        return self.create_branch(branch_name)

    def stage_all(self) -> GitResult[None]:
        """Stage all changes."""
        return self._run(["add", "-A"])

    def commit(self, message: str) -> GitResult[None]:
        """Create a commit."""
        self.stage_all()
        status = self._run(["status", "--porcelain"])
        if not status.stdout.strip():
            return GitResult(
                success=False,
                returncode=1,
                stdout="",
                stderr="No changes to commit",
                command=["git", "commit", "-m", message],
            )
        return self._run(["commit", "-m", message])

    def push(self, branch_name: str, force: bool = False) -> GitResult[None]:
        """Push branch to origin."""
        args = ["push", "-u", "origin", branch_name]
        if force:
            args.insert(1, "--force-with-lease")
        return self._run(args)

    def get_diff(self, base_branch: str | None = None) -> str:
        """Get diff against base branch."""
        if base_branch is None:
            base_branch = self.get_default_branch()
        result = self._run(["diff", base_branch])
        return result.stdout if result.success else ""

    def get_status(self) -> str:
        """Get git status."""
        result = self._run(["status"])
        return result.stdout if result.success else ""

    def cleanup_branch(self, branch_name: str) -> None:
        """Clean up a branch (local and remote)."""
        default_branch = self.get_default_branch()
        self._run(["checkout", default_branch], check=False)
        self._run(["branch", "-D", branch_name], check=False)
        self._run(["push", "origin", "--delete", branch_name], check=False)

    def build_worktree_path(self, issue_number: int) -> Path:
        """Build a unique worktree path for an issue."""
        repo_slug = self.repo.replace("/", "-")
        suffix = f"{os.getpid()}-{time.time_ns()}"
        name = f"{repo_slug}-issue-{issue_number}-{suffix}"
        return self.workspace.parent / name

    def create_worktree(self, issue_number: int, branch_name: str) -> GitResult[Path]:
        """Create a dedicated worktree and branch for issue processing."""
        path = self.build_worktree_path(issue_number)
        path.parent.mkdir(parents=True, exist_ok=True)
        base_branch = self.get_default_branch()
        result = self._run(
            ["worktree", "add", "-b", branch_name, str(path), base_branch]
        )
        return GitResult(
            success=result.success,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            command=result.command,
            value=path if result.success else None,
        )

    def remove_worktree(self, path: Path, force: bool = False) -> GitResult[None]:
        """Remove a worktree path."""
        args = ["worktree", "remove"]
        if force:
            args.append("-f")
        args.append(str(path))
        return self._run(args)

    @property
    def path(self) -> Path:
        """Get current workspace path."""
        return self.workspace
