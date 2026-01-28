#!/usr/bin/env python3
"""Worker Agent - Autonomous implementation daemon.

Watches for issues with 'status:ready' label and automatically
implements them using the configured LLM backend (codex or claude).
"""

import argparse
import logging
import sys
import time
from pathlib import Path

# Add parent directory to path for shared imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from git_operations import GitOperations

from shared.config import get_agent_config
from shared.github_client import GitHubClient, Issue
from shared.llm_client import LLMClient
from shared.lock import LockManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("worker-agent")


class WorkerAgent:
    """Autonomous worker that implements issues."""

    # Status labels
    STATUS_READY = "status:ready"
    STATUS_IMPLEMENTING = "status:implementing"
    STATUS_REVIEWING = "status:reviewing"
    STATUS_FAILED = "status:failed"

    def __init__(self, repo: str, config_path: str | None = None):
        self.repo = repo
        self.config = get_agent_config(repo, config_path)

        # Initialize components
        self.github = GitHubClient(repo, gh_cli=self.config.gh_cli)
        self.lock = LockManager(self.github, agent_type="worker")
        self.llm = LLMClient(self.config)
        self.git = GitOperations(repo, Path(self.config.work_dir))

        logger.info(f"Worker Agent initialized for {repo}")
        logger.info(f"LLM backend: {self.config.llm_backend}")
        logger.info(f"Work directory: {self.git.workspace}")

    def run(self) -> None:
        """Main daemon loop."""
        logger.info(f"Starting Worker Agent daemon for {self.repo}")
        logger.info(f"Poll interval: {self.config.poll_interval}s")

        while True:
            try:
                self._process_ready_issues()
                time.sleep(self.config.poll_interval)

            except KeyboardInterrupt:
                logger.info("Shutting down Worker Agent")
                break
            except Exception as e:
                logger.exception(f"Unexpected error: {e}")
                time.sleep(60)  # Wait before retry

    def run_once(self) -> bool:
        """Process one issue and return. For testing."""
        issues = self.github.list_issues(labels=[self.STATUS_READY])
        if not issues:
            logger.info("No ready issues found")
            return False

        return self._try_process_issue(issues[0])

    def _process_ready_issues(self) -> None:
        """Find and process ready issues."""
        issues = self.github.list_issues(labels=[self.STATUS_READY])

        if not issues:
            logger.debug("No ready issues found")
            return

        logger.info(f"Found {len(issues)} ready issue(s)")

        for issue in issues:
            if self._try_process_issue(issue):
                logger.info(f"Successfully processed issue #{issue.number}")
            else:
                logger.debug(f"Skipped issue #{issue.number}")

    def _try_process_issue(self, issue: Issue) -> bool:
        """
        Try to process a single issue.

        Returns True if processed successfully, False otherwise.
        """
        logger.info(f"Attempting to process issue #{issue.number}: {issue.title}")

        # Try to acquire lock
        lock_result = self.lock.try_lock_issue(
            issue.number,
            self.STATUS_READY,
            self.STATUS_IMPLEMENTING,
        )

        if not lock_result.success:
            logger.debug(f"Could not lock issue #{issue.number}: {lock_result.error}")
            return False

        try:
            # Clone/update repository
            logger.info("Preparing workspace...")
            clone_result = self.git.clone_or_pull()
            if not clone_result.success:
                raise RuntimeError(f"Failed to prepare workspace: {clone_result.error}")

            # Create feature branch
            branch_name = f"auto/issue-{issue.number}"
            logger.info(f"Creating branch: {branch_name}")

            branch_result = self.git.create_branch(branch_name)
            if not branch_result.success:
                raise RuntimeError(f"Failed to create branch: {branch_result.error}")

            # Generate implementation
            logger.info(f"Generating implementation with {self.config.llm_backend}...")
            gen_result = self.llm.generate_implementation(
                spec=issue.body,
                repo_context=f"Repository: {self.repo}",
                work_dir=self.git.path,
            )

            if not gen_result.success:
                raise RuntimeError(f"Implementation failed: {gen_result.error}")

            # Commit changes
            logger.info("Committing changes...")
            commit_msg = f"feat: implement #{issue.number}\n\n{issue.title}"
            commit_result = self.git.commit(commit_msg)

            if not commit_result.success:
                raise RuntimeError(f"Commit failed: {commit_result.error}")

            # Push branch
            logger.info("Pushing to remote...")
            push_result = self.git.push(branch_name)
            if not push_result.success:
                raise RuntimeError(f"Push failed: {push_result.error}")

            # Create pull request
            logger.info("Creating pull request...")
            pr_body = f"""## Summary
Auto-generated implementation for #{issue.number}

## Original Issue
{issue.title}

## Implementation
Generated by Worker Agent using {self.config.llm_backend}.

Closes #{issue.number}

---
🤖 Auto-generated by Workflow Engine Worker Agent
"""

            # Get default branch for PR base
            default_branch = self.github.get_default_branch()

            pr_url = self.github.create_pr(
                title=f"Auto: {issue.title}",
                body=pr_body,
                head=branch_name,
                base=default_branch,
                labels=[self.STATUS_REVIEWING],
            )

            if not pr_url:
                raise RuntimeError("Failed to create pull request")

            logger.info(f"Pull request created: {pr_url}")

            # Comment on issue
            self.github.comment_issue(
                issue.number,
                f"✅ Implementation complete!\n\nPull Request: {pr_url}",
            )

            return True

        except Exception as e:
            logger.error(f"Failed to process issue #{issue.number}: {e}")

            # Mark as failed
            self.lock.mark_failed(
                issue.number,
                self.STATUS_IMPLEMENTING,
                str(e),
            )

            # Cleanup branch if it was created
            self.git.cleanup_branch(f"auto/issue-{issue.number}")

            return False


def main():
    parser = argparse.ArgumentParser(
        description="Worker Agent - Autonomous implementation daemon"
    )
    parser.add_argument(
        "repo",
        help="Repository in owner/repo format",
    )
    parser.add_argument(
        "--config",
        "-c",
        help="Path to config file",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process one issue and exit",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    agent = WorkerAgent(args.repo, config_path=args.config)

    if args.once:
        success = agent.run_once()
        sys.exit(0 if success else 1)
    else:
        agent.run()


if __name__ == "__main__":
    main()
