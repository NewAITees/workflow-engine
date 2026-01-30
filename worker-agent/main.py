#!/usr/bin/env python3
"""Worker Agent - Autonomous implementation daemon.

Watches for issues with 'status:ready' label and automatically
implements them using the configured LLM backend (codex or claude).
"""

import argparse
import logging
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

# Add parent directory to path for shared imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.config import get_agent_config
from shared.git_operations import GitOperations
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
    STATUS_TESTING = "status:testing"
    STATUS_REVIEWING = "status:reviewing"
    STATUS_CHANGES_REQUESTED = "status:changes-requested"
    STATUS_FAILED = "status:failed"
    STATUS_CI_FAILED = "status:ci-failed"

    # Retry settings
    MAX_RETRIES = 3
    MAX_CI_RETRIES = 3

    # CI settings
    CI_WAIT_TIMEOUT = 600  # 10 minutes
    CI_CHECK_INTERVAL = 30  # 30 seconds

    def __init__(self, repo: str, config_path: str | None = None):
        self.repo = repo
        self.config = get_agent_config(repo, config_path)
        assert self.config.work_dir is not None

        # Generate unique agent ID
        self.agent_id = f"worker-{uuid.uuid4().hex[:8]}"

        # Initialize components
        self.github = GitHubClient(repo, gh_cli=self.config.gh_cli)
        self.lock = LockManager(
            self.github, agent_type="worker", agent_id=self.agent_id
        )
        self.llm = LLMClient(self.config)
        self.git = GitOperations(repo, Path(self.config.work_dir))

        logger.info(f"Worker Agent initialized for {repo}")
        logger.info(f"Agent ID: {self.agent_id}")
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
        Try to process a single issue with TDD flow.

        TDD Flow:
        1. Generate tests first
        2. Generate implementation
        3. Run tests
        4. Retry on failure (max 3 times)

        Returns True if processed successfully, False otherwise.
        """
        logger.info(
            f"[{self.agent_id}] Attempting to process issue #{issue.number}: {issue.title}"
        )

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
            logger.info(f"[{self.agent_id}] Preparing workspace...")
            clone_result = self.git.clone_or_pull()
            if not clone_result.success:
                raise RuntimeError(f"Failed to prepare workspace: {clone_result.error}")

            # Create feature branch
            branch_name = f"auto/issue-{issue.number}"
            logger.info(f"[{self.agent_id}] Creating branch: {branch_name}")

            branch_result = self.git.create_branch(branch_name)
            if not branch_result.success:
                raise RuntimeError(f"Failed to create branch: {branch_result.error}")

            # TDD Step 1: Generate tests first
            logger.info(
                f"[{self.agent_id}] Generating tests with {self.config.llm_backend}..."
            )
            test_result = self.llm.generate_tests(
                spec=issue.body,
                repo_context=f"Repository: {self.repo}",
                work_dir=self.git.path,
            )

            if not test_result.success:
                raise RuntimeError(f"Test generation failed: {test_result.error}")

            # Commit tests
            logger.info(f"[{self.agent_id}] Committing tests...")
            test_commit_msg = f"test: add tests for #{issue.number}\n\n{issue.title}"
            test_commit_result = self.git.commit(test_commit_msg)

            if not test_commit_result.success:
                raise RuntimeError(f"Test commit failed: {test_commit_result.error}")

            # TDD Step 2-4: Implementation with test retry loop
            test_retry_count = 0
            test_failure_output = ""
            test_passed = False

            while test_retry_count < self.MAX_RETRIES:
                logger.info(
                    f"[{self.agent_id}] Generating implementation "
                    f"(attempt {test_retry_count + 1}/{self.MAX_RETRIES})..."
                )

                # Prepare spec with test feedback if retrying
                impl_spec = issue.body
                if test_retry_count > 0:
                    impl_spec += (
                        f"\n\n## Previous Test Failure (Attempt {test_retry_count})\n"
                        f"Please fix the implementation to pass the tests.\n\n"
                        f"```\n{test_failure_output[:2000]}\n```"
                    )

                # Generate implementation
                gen_result = self.llm.generate_implementation(
                    spec=impl_spec,
                    repo_context=f"Repository: {self.repo}\nTDD attempt: {test_retry_count + 1}/{self.MAX_RETRIES}",
                    work_dir=self.git.path,
                )

                if not gen_result.success:
                    raise RuntimeError(f"Implementation failed: {gen_result.error}")

                # Commit implementation
                logger.info(f"[{self.agent_id}] Committing implementation...")
                impl_commit_msg = f"feat: implement #{issue.number} (attempt {test_retry_count + 1})\n\n{issue.title}"
                impl_commit_result = self.git.commit(impl_commit_msg)

                if not impl_commit_result.success:
                    raise RuntimeError(
                        f"Implementation commit failed: {impl_commit_result.error}"
                    )

                # Transition to testing status
                self.github.remove_label(issue.number, self.STATUS_IMPLEMENTING)
                self.github.add_label(issue.number, self.STATUS_TESTING)

                # Run tests
                logger.info(f"[{self.agent_id}] Running tests...")
                test_passed, test_output = self._run_tests(issue.number)

                if test_passed:
                    logger.info(
                        f"[{self.agent_id}] Tests passed on attempt {test_retry_count + 1}"
                    )
                    break

                # Tests failed - record and retry
                test_retry_count += 1
                test_failure_output = test_output

                logger.warning(
                    f"[{self.agent_id}] Tests failed (attempt {test_retry_count}/{self.MAX_RETRIES})"
                )

                # Comment on issue about test failure
                self.github.comment_issue(
                    issue.number,
                    f"⚠️ **Test failed (attempt {test_retry_count}/{self.MAX_RETRIES})**\n\n"
                    f"Retrying implementation with test feedback...\n\n"
                    f"<details>\n<summary>Test output</summary>\n\n"
                    f"```\n{test_output[:2000]}\n```\n\n</details>",
                )

                if test_retry_count >= self.MAX_RETRIES:
                    # Max retries exceeded
                    raise RuntimeError(
                        f"Tests failed after {self.MAX_RETRIES} attempts:\n{test_output[:500]}"
                    )

                # Transition back to implementing for retry
                self.github.remove_label(issue.number, self.STATUS_TESTING)
                self.github.add_label(issue.number, self.STATUS_IMPLEMENTING)

            # Tests passed! Transition to reviewing
            self.github.remove_label(issue.number, self.STATUS_TESTING)

            # Push branch
            logger.info(f"[{self.agent_id}] Pushing to remote...")
            push_result = self.git.push(branch_name)
            if not push_result.success:
                raise RuntimeError(f"Push failed: {push_result.error}")

            # Create pull request
            logger.info(f"[{self.agent_id}] Creating pull request...")
            pr_body = f"""## Summary
Auto-generated implementation for #{issue.number} using TDD approach.

## Original Issue
{issue.title}

## Implementation
Generated by Worker Agent ({self.agent_id}) using {self.config.llm_backend}.

**TDD Process:**
- ✅ Tests generated first
- ✅ Implementation created
- ✅ All tests passed (attempts: {test_retry_count + 1}/{self.MAX_RETRIES})

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

            logger.info(f"[{self.agent_id}] Pull request created: {pr_url}")

            # Comment on issue
            self.github.comment_issue(
                issue.number,
                f"✅ **Implementation complete with TDD!**\n\n"
                f"- Tests generated and passed\n"
                f"- Attempts: {test_retry_count + 1}/{self.MAX_RETRIES}\n\n"
                f"Pull Request: {pr_url}",
            )

            return True

        except Exception as e:
            logger.error(
                f"[{self.agent_id}] Failed to process issue #{issue.number}: {e}"
            )

            # Mark as failed
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
        Try to retry a PR with changes requested using TDD flow.

        Returns True if retried successfully, False otherwise.
        """
        logger.info(
            f"[{self.agent_id}] Attempting to retry PR #{pr.number}: {pr.title}"
        )

        # Get retry count
        retry_count = self._get_retry_count(pr.number)
        logger.info(
            f"[{self.agent_id}] PR #{pr.number} retry count: {retry_count}/{self.MAX_RETRIES}"
        )

        if retry_count >= self.MAX_RETRIES:
            logger.warning(
                f"[{self.agent_id}] PR #{pr.number} exceeded max retries, escalating to Planner"
            )
            # Extract issue number
            issue_number = self._extract_issue_number(pr.body)
            if issue_number:
                self.github.comment_issue(
                    issue_number,
                    f"⚠️ **Auto-retry failed after {self.MAX_RETRIES} attempts**\n\n"
                    f"PR #{pr.number} could not pass review.\n\n"
                    f"**Action needed**: Please review the specification and provide more details or clarification.\n\n"
                    f"Review feedback:\n{self._get_latest_review_feedback(pr.number)}",
                )
                # Remove changes-requested label and add failed
                self.github.remove_pr_label(pr.number, self.STATUS_CHANGES_REQUESTED)
                self.github.add_pr_label(pr.number, self.STATUS_FAILED)
            return False

        # Try to acquire lock (changes-requested -> implementing)
        lock_result = self.lock.try_lock_pr(
            pr.number,
            self.STATUS_CHANGES_REQUESTED,
            self.STATUS_IMPLEMENTING,
        )

        if not lock_result.success:
            logger.debug(f"Could not lock PR #{pr.number}: {lock_result.error}")
            return False

        try:
            # Get the linked issue
            issue_number = self._extract_issue_number(pr.body)
            if not issue_number:
                raise RuntimeError("Cannot find linked issue in PR body")

            issue = self.github.get_issue(issue_number)
            if not issue:
                raise RuntimeError(f"Issue #{issue_number} not found")

            # Get review feedback
            feedback = self._get_latest_review_feedback(pr.number)

            # Prepare workspace
            logger.info(f"[{self.agent_id}] Preparing workspace...")
            clone_result = self.git.clone_or_pull()
            if not clone_result.success:
                raise RuntimeError(f"Failed to prepare workspace: {clone_result.error}")

            # Update the branch
            branch_name = pr.head_ref
            logger.info(f"[{self.agent_id}] Updating branch: {branch_name}")
            branch_result = self.git.create_branch(branch_name)
            if not branch_result.success:
                raise RuntimeError(f"Failed to update branch: {branch_result.error}")

            # TDD retry loop with test execution
            test_retry_count = 0
            test_failure_output = ""
            test_passed = False

            while test_retry_count < self.MAX_RETRIES:
                logger.info(
                    f"[{self.agent_id}] Regenerating implementation with feedback "
                    f"(test attempt {test_retry_count + 1}/{self.MAX_RETRIES})..."
                )

                # Prepare spec with review feedback and test feedback
                impl_spec = f"{issue.body}\n\n## Review Feedback (Please address these issues)\n{feedback}"

                if test_retry_count > 0:
                    impl_spec += (
                        f"\n\n## Test Failure (Attempt {test_retry_count})\n"
                        f"Please fix the implementation to pass the tests.\n\n"
                        f"```\n{test_failure_output[:2000]}\n```"
                    )

                # Generate improved implementation
                gen_result = self.llm.generate_implementation(
                    spec=impl_spec,
                    repo_context=f"Repository: {self.repo}\n"
                    f"Review retry: {retry_count + 1}/{self.MAX_RETRIES}\n"
                    f"Test attempt: {test_retry_count + 1}/{self.MAX_RETRIES}",
                    work_dir=self.git.path,
                )

                if not gen_result.success:
                    raise RuntimeError(f"Implementation failed: {gen_result.error}")

                # Commit changes
                logger.info(f"[{self.agent_id}] Committing changes...")
                commit_msg = (
                    f"fix: address review feedback for #{issue.number} "
                    f"(retry {retry_count + 1}, test attempt {test_retry_count + 1})\n\n"
                    f"{feedback[:500]}"
                )
                commit_result = self.git.commit(commit_msg)

                if not commit_result.success:
                    raise RuntimeError(f"Commit failed: {commit_result.error}")

                # Transition to testing status
                self.github.remove_pr_label(pr.number, self.STATUS_IMPLEMENTING)
                self.github.add_pr_label(pr.number, self.STATUS_TESTING)

                # Run tests
                logger.info(f"[{self.agent_id}] Running tests...")
                test_passed, test_output = self._run_tests(issue_number)

                if test_passed:
                    logger.info(
                        f"[{self.agent_id}] Tests passed on attempt {test_retry_count + 1}"
                    )
                    break

                # Tests failed - record and retry
                test_retry_count += 1
                test_failure_output = test_output

                logger.warning(
                    f"[{self.agent_id}] Tests failed (attempt {test_retry_count}/{self.MAX_RETRIES})"
                )

                # Comment on PR about test failure
                self.github.comment_pr(
                    pr.number,
                    f"⚠️ **Test failed during retry (attempt {test_retry_count}/{self.MAX_RETRIES})**\n\n"
                    f"Retrying implementation with test feedback...\n\n"
                    f"<details>\n<summary>Test output</summary>\n\n"
                    f"```\n{test_output[:2000]}\n```\n\n</details>",
                )

                if test_retry_count >= self.MAX_RETRIES:
                    raise RuntimeError(
                        f"Tests failed after {self.MAX_RETRIES} attempts:\n{test_output[:500]}"
                    )

                # Transition back to implementing for retry
                self.github.remove_pr_label(pr.number, self.STATUS_TESTING)
                self.github.add_pr_label(pr.number, self.STATUS_IMPLEMENTING)

            # Tests passed! Remove testing label
            self.github.remove_pr_label(pr.number, self.STATUS_TESTING)

            # Force push to update PR
            logger.info(f"[{self.agent_id}] Force pushing to update PR...")
            push_result = self.git.push(branch_name, force=True)
            if not push_result.success:
                raise RuntimeError(f"Push failed: {push_result.error}")

            # Record retry
            self._record_retry(pr.number, retry_count + 1)

            # Add reviewing label
            self.github.add_pr_label(pr.number, self.STATUS_REVIEWING)

            logger.info(
                f"[{self.agent_id}] PR #{pr.number} updated with TDD, ready for re-review"
            )

            # Comment on PR
            self.github.comment_pr(
                pr.number,
                f"✅ **Implementation updated with TDD!**\n\n"
                f"- Addressed review feedback\n"
                f"- All tests passed (attempts: {test_retry_count + 1}/{self.MAX_RETRIES})\n"
                f"- Ready for re-review\n\n"
                f"Review retry: {retry_count + 1}/{self.MAX_RETRIES}",
            )

            return True

        except Exception as e:
            logger.error(f"[{self.agent_id}] Failed to retry PR #{pr.number}: {e}")

            # Reset to changes-requested
            self.github.remove_pr_label(pr.number, self.STATUS_IMPLEMENTING)
            self.github.remove_pr_label(pr.number, self.STATUS_TESTING)
            self.github.add_pr_label(pr.number, self.STATUS_CHANGES_REQUESTED)

            self.github.comment_pr(
                pr.number,
                f"⚠️ Auto-retry failed: {e}\n\nPlease review manually or update the specification.",
            )

            return False

    def _run_tests(self, issue_number: int) -> tuple[bool, str]:
        """
        Run pytest for the specific issue's tests.

        Args:
            issue_number: The issue number

        Returns:
            (success, output): 成功フラグとテスト出力
        """
        test_file = f"tests/test_issue_{issue_number}.py"
        test_path = self.git.path / test_file

        if not test_path.exists():
            return False, f"Test file not found: {test_file}"

        logger.info(f"[{self.agent_id}] Running tests: {test_file}")

        try:
            result = subprocess.run(
                ["pytest", test_file, "-v", "--tb=short"],
                cwd=self.git.path,
                capture_output=True,
                text=True,
                timeout=300,  # 5分タイムアウト
            )

            output = f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}"
            success = result.returncode == 0

            if success:
                logger.info(f"[{self.agent_id}] Tests passed for issue #{issue_number}")
            else:
                logger.warning(
                    f"[{self.agent_id}] Tests failed for issue #{issue_number}"
                )

            return success, output

        except subprocess.TimeoutExpired:
            error_msg = "Tests timed out after 5 minutes"
            logger.error(f"[{self.agent_id}] {error_msg}")
            return False, error_msg
        except Exception as e:
            error_msg = f"Test execution error: {str(e)}"
            logger.error(f"[{self.agent_id}] {error_msg}")
            return False, error_msg

    def _wait_for_ci(
        self, pr_number: int, timeout: int | None = None
    ) -> tuple[bool, str]:
        """
        Wait for CI to complete (max timeout seconds).

        Args:
            pr_number: PR number
            timeout: Maximum wait time in seconds (default: CI_WAIT_TIMEOUT)

        Returns:
            (passed, status):
                - (True, "success") - CI passed
                - (False, "pending") - CI timeout (still pending)
                - (False, "failure") - CI failed
        """
        if timeout is None:
            timeout = self.CI_WAIT_TIMEOUT

        logger.info(
            f"[{self.agent_id}] Waiting for CI on PR #{pr_number} (timeout: {timeout}s)"
        )

        elapsed = 0
        while elapsed < timeout:
            ci_status = self.github.get_ci_status(pr_number)

            if ci_status["status"] == "success":
                logger.info(f"[{self.agent_id}] CI passed on PR #{pr_number}")
                return True, "success"
            elif ci_status["status"] == "failure":
                logger.warning(f"[{self.agent_id}] CI failed on PR #{pr_number}")
                return False, "failure"
            elif ci_status["status"] == "none":
                logger.info(f"[{self.agent_id}] No CI configured for PR #{pr_number}")
                return True, "success"  # No CI means pass

            # Still pending, wait and check again
            pending_count = ci_status["pending_count"]
            logger.debug(
                f"[{self.agent_id}] CI pending on PR #{pr_number} "
                f"({pending_count} checks pending, elapsed: {elapsed}s)"
            )

            time.sleep(self.CI_CHECK_INTERVAL)
            elapsed += self.CI_CHECK_INTERVAL

        # Timeout
        logger.warning(
            f"[{self.agent_id}] CI timeout on PR #{pr_number} after {timeout}s"
        )
        return False, "pending"

    def _get_ci_failure_logs(self, pr_number: int) -> str:
        """
        Get formatted CI failure logs for LLM.

        Args:
            pr_number: PR number

        Returns:
            Formatted CI error logs
        """
        logger.info(f"[{self.agent_id}] Fetching CI failure logs for PR #{pr_number}")

        failed_checks = self.github.get_ci_logs(pr_number)

        if not failed_checks:
            return "CI failed but no detailed logs available."

        # Format logs for LLM
        logs = []
        logs.append("# CI Failure Report\n")

        for check in failed_checks:
            logs.append(f"\n## Check: {check['name']}")
            logs.append(f"**Status:** {check['conclusion']}")

            if "html_url" in check:
                logs.append(f"**URL:** {check['html_url']}")

            if "output" in check and check["output"]:
                output = check["output"]
                if output.get("title"):
                    logs.append(f"\n**Error:** {output['title']}")
                if output.get("summary"):
                    logs.append(f"\n**Details:**\n```\n{output['summary'][:1000]}\n```")

        return "\n".join(logs)

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

    def _get_retry_count(self, pr_number: int) -> int:
        """Get current retry count from PR comments."""
        comments = self.github.get_issue_comments(pr_number, limit=50)
        retry_count = 0

        for comment in comments:
            body = comment.get("body", "")
            # Look for RETRY:N marker
            match = re.search(r"RETRY:(\d+)", body)
            if match:
                count = int(match.group(1))
                retry_count = max(retry_count, count)

        return retry_count

    def _record_retry(self, pr_number: int, retry_count: int) -> None:
        """Record retry attempt in PR comment."""
        self.github.comment_pr(
            pr_number,
            f"🔄 **Auto-retry #{retry_count}**\n\nRETRY:{retry_count}\n\n"
            f"Worker Agent is addressing review feedback and regenerating implementation.",
        )

    def _get_latest_review_feedback(self, pr_number: int) -> str:
        """Get the most recent review feedback."""
        reviews = self.github.get_pr_reviews(pr_number)

        if not reviews:
            return "No review feedback available"

        # Get the most recent CHANGES_REQUESTED review
        for review in reversed(reviews):
            if review.get("state") == "CHANGES_REQUESTED":
                body = review.get("body", "").strip()
                if body:
                    return str(body)

        # Fallback to the most recent review with a body
        for review in reversed(reviews):
            body = review.get("body", "").strip()
            if body:
                return str(body)

        return "No detailed review feedback available"


def main() -> None:
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
