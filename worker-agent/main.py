#!/usr/bin/env python3
"""Worker Agent - Autonomous implementation daemon.

Watches for issues with 'status:ready' label and automatically
implements them using the configured LLM backend (codex or claude).
"""

import argparse
import logging
import re
import sys
import time
from pathlib import Path

# Add parent directory to path for shared imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from git_operations import GitOperations

from shared.config import get_agent_config
from shared.github_client import GitHubClient, Issue, PullRequest
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
    STATUS_CHANGES_REQUESTED = "status:changes-requested"
    STATUS_FAILED = "status:failed"

    # Retry settings
    MAX_RETRIES = 3
    RETRY_MARKER_RE = re.compile(r"<!--\s*worker-retry-count:(\d+)\s*-->")

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
                self._process_changes_requested_prs()
                time.sleep(self.config.poll_interval)

            except KeyboardInterrupt:
                logger.info("Shutting down Worker Agent")
                break
            except Exception as e:
                logger.exception(f"Unexpected error: {e}")
                time.sleep(60)  # Wait before retry

    def run_once(self) -> bool:
        """Process one issue/PR and return. For testing."""
        # Try ready issues first
        issues = self.github.list_issues(labels=[self.STATUS_READY])
        if issues:
            return self._try_process_issue(issues[0])

        # Then try changes-requested PRs
        prs = self.github.list_prs(labels=[self.STATUS_CHANGES_REQUESTED])
        if prs:
            return self._try_retry_pr(prs[0])

        logger.info("No work to process")
        return False

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
            logger.exception(f"Failed to process issue #{issue.number}: {e}")

            # Mark as failed
            self._comment_planner_escalation(
                issue.number,
                f"Implementation failed: {e}",
            )
            self.lock.mark_failed(
                issue.number,
                self.STATUS_IMPLEMENTING,
                str(e),
            )

            # Cleanup branch if it was created
            self.git.cleanup_branch(f"auto/issue-{issue.number}")

            return False

    def _process_changes_requested_prs(self) -> None:
        """Find and retry PRs with changes requested."""
        prs = self.github.list_prs(labels=[self.STATUS_CHANGES_REQUESTED])

        if not prs:
            logger.debug("No PRs with changes requested")
            return

        logger.info(f"Found {len(prs)} PR(s) with changes requested")

        for pr in prs:
            if self._try_retry_pr(pr):
                logger.info(f"Successfully retried PR #{pr.number}")

    def _try_retry_pr(self, pr: PullRequest) -> bool:
        """
        Try to retry a PR with changes requested.

        Returns True if retried successfully, False otherwise.
        """
        logger.info(f"Attempting to retry PR #{pr.number}: {pr.title}")

        # Try to acquire lock (changes-requested -> implementing)
        lock_result = self.lock.try_lock_pr(
            pr.number,
            self.STATUS_CHANGES_REQUESTED,
            self.STATUS_IMPLEMENTING,
        )

        if not lock_result.success:
            logger.debug(f"Could not lock PR #{pr.number}: {lock_result.error}")
            return False

        issue_number: int | None = None
        issue_lock_acquired = False
        issue_failed = False

        try:
            # Get the linked issue
            issue_number = self._extract_issue_number(pr.body)
            if not issue_number:
                self.github.comment_pr(
                    pr.number,
                    "⚠️ Auto-retry skipped: cannot find linked issue in PR body.",
                )
                self._reset_pr_labels(
                    pr.number,
                    self.STATUS_IMPLEMENTING,
                    self.STATUS_CHANGES_REQUESTED,
                )
                return False

            issue_lock = self.lock.try_lock_issue(
                issue_number,
                self.STATUS_CHANGES_REQUESTED,
                self.STATUS_IMPLEMENTING,
            )
            if not issue_lock.success:
                self._reset_pr_labels(
                    pr.number,
                    self.STATUS_IMPLEMENTING,
                    self.STATUS_CHANGES_REQUESTED,
                )
                return False
            issue_lock_acquired = True

            issue = self.github.get_issue(issue_number)
            if not issue:
                raise RuntimeError(f"Issue #{issue_number} not found")

            retry_count = self._get_retry_count(issue_number)
            logger.info(
                f"Issue #{issue_number} retry count: {retry_count}/{self.MAX_RETRIES}"
            )

            if retry_count >= self.MAX_RETRIES:
                logger.warning(
                    f"Issue #{issue_number} exceeded max retries, escalating to Planner"
                )
                self._comment_retry_exhausted(
                    issue_number,
                    pr.number,
                    self._get_latest_review_feedback(pr.number),
                )
                self._reset_pr_labels(
                    pr.number,
                    self.STATUS_IMPLEMENTING,
                    self.STATUS_FAILED,
                )
                return False

            next_retry = retry_count + 1
            self.github.comment_issue(
                issue_number,
                self._build_retry_comment(next_retry, pr.number),
            )

            # Get review feedback
            feedback = self._get_latest_review_feedback(pr.number)

            # Prepare workspace
            logger.info("Preparing workspace...")
            clone_result = self.git.clone_or_pull()
            if not clone_result.success:
                raise RuntimeError(f"Failed to prepare workspace: {clone_result.error}")

            # Update the branch
            branch_name = pr.head_ref
            logger.info(f"Updating branch: {branch_name}")
            branch_result = self.git.create_branch(branch_name)
            if not branch_result.success:
                raise RuntimeError(f"Failed to update branch: {branch_result.error}")

            # Generate improved implementation with feedback
            logger.info(f"Regenerating implementation with feedback (retry {retry_count + 1})...")
            gen_result = self.llm.generate_implementation(
                spec=f"{issue.body}\n\n## Review Feedback (Please address these issues)\n{feedback}",
                repo_context=f"Repository: {self.repo}\nRetry attempt: {retry_count + 1}/{self.MAX_RETRIES}",
                work_dir=self.git.path,
            )

            if not gen_result.success:
                raise RuntimeError(f"Implementation failed: {gen_result.error}")

            # Commit changes
            logger.info("Committing changes...")
            commit_msg = f"fix: address review feedback for #{issue.number} (retry {retry_count + 1})\n\n{feedback[:500]}"
            commit_result = self.git.commit(commit_msg)

            if not commit_result.success:
                raise RuntimeError(f"Commit failed: {commit_result.error}")

            # Force push to update PR
            logger.info("Force pushing to update PR...")
            push_result = self.git.push(branch_name, force=True)
            if not push_result.success:
                raise RuntimeError(f"Push failed: {push_result.error}")

            # Record retry
            # Update labels: implementing -> reviewing
            self._reset_pr_labels(
                pr.number,
                self.STATUS_IMPLEMENTING,
                self.STATUS_REVIEWING,
            )

            logger.info(f"PR #{pr.number} updated, ready for re-review")
            return True

        except Exception as e:
            logger.exception(f"Failed to retry PR #{pr.number}: {e}")

            if issue_number:
                self._comment_planner_escalation(
                    issue_number,
                    f"Retry implementation failed: {e}",
                )
                self.lock.mark_failed(
                    issue_number,
                    self.STATUS_IMPLEMENTING,
                    str(e),
                )
                issue_failed = True

            try:
                self._reset_pr_labels(
                    pr.number,
                    self.STATUS_IMPLEMENTING,
                    self.STATUS_CHANGES_REQUESTED,
                )
            except Exception as label_err:
                logger.warning(
                    f"Failed to reset labels for PR #{pr.number}: {label_err}"
                )

            return False
        finally:
            if issue_lock_acquired and issue_number and not issue_failed:
                self._release_issue_lock(issue_number)

    def _extract_issue_number(self, pr_body: str) -> int | None:
        """Extract issue number from PR body."""
        patterns = [
            r"[Cc]loses?\s+#(\d+)",
            r"[Ff]ixes?\s+#(\d+)",
            r"[Rr]esolves?\s+#(\d+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, pr_body)
            if match:
                return int(match.group(1))

        return None

    def _get_retry_count(self, issue_number: int) -> int:
        """Get current retry count from issue comments."""
        comments = self.github.get_issue_comments(issue_number, limit=50)
        return self._parse_retry_count(comments)

    @staticmethod
    def _parse_retry_count(comments: list[dict]) -> int:
        """Parse retry count from issue comments."""
        retry_count = 0
        for comment in comments:
            body = comment.get("body", "")
            match = WorkerAgent.RETRY_MARKER_RE.search(body)
            if match:
                retry_count = max(retry_count, int(match.group(1)))
        return retry_count

    @staticmethod
    def _build_retry_comment(retry_count: int, pr_number: int) -> str:
        """Build a retry comment with a stable marker."""
        return (
            f"🔄 **Auto-retry #{retry_count}**\n\n"
            f"PR #{pr_number} is being re-implemented based on review feedback.\n\n"
            f"<!-- worker-retry-count:{retry_count} -->"
        )

    def _comment_retry_exhausted(
        self, issue_number: int, pr_number: int, feedback: str
    ) -> None:
        """Comment on the issue when retries are exhausted."""
        self.github.comment_issue(
            issue_number,
            f"⚠️ **Auto-retry failed after {self.MAX_RETRIES} attempts**\n\n"
            f"PR #{pr_number} could not pass review.\n\n"
            f"**Action needed**: Please review the specification and provide more details or clarification.\n\n"
            f"Review feedback:\n{feedback}",
        )

    def _comment_planner_escalation(self, issue_number: int, reason: str) -> None:
        """Escalate to Planner for design/spec review."""
        self.github.comment_issue(
            issue_number,
            f"⚠️ **Planner escalation requested**\n\n{reason}\n\n"
            "Please review the design/spec and clarify requirements.",
        )

    def _reset_pr_labels(self, pr_number: int, from_label: str, to_label: str) -> None:
        """Reset PR labels during retry flow."""
        self.github.remove_pr_label(pr_number, from_label)
        self.github.add_pr_label(pr_number, to_label)

    def _release_issue_lock(self, issue_number: int) -> None:
        """Release issue lock by restoring changes-requested label."""
        self.github.remove_label(issue_number, self.STATUS_IMPLEMENTING)
        self.github.add_label(issue_number, self.STATUS_CHANGES_REQUESTED)

    def _get_latest_review_feedback(self, pr_number: int) -> str:
        """Get the most recent review feedback."""
        reviews = self.github.get_pr_reviews(pr_number)

        if not reviews:
            comments = self.github.get_issue_comments(pr_number, limit=10)
            for comment in reversed(comments):
                body = comment.get("body", "").strip()
                if body:
                    return body
            return "No review feedback available"

        # Get the most recent CHANGES_REQUESTED review
        for review in reversed(reviews):
            if review.get("state") == "CHANGES_REQUESTED":
                body = review.get("body", "").strip()
                if body:
                    return body

        # Fallback to the most recent review with a body
        for review in reversed(reviews):
            body = review.get("body", "").strip()
            if body:
                return body

        comments = self.github.get_issue_comments(pr_number, limit=10)
        for comment in reversed(comments):
            body = comment.get("body", "").strip()
            if body:
                return body

        return "No detailed review feedback available"


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
