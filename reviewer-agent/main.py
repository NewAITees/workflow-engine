#!/usr/bin/env python3
"""Reviewer Agent - Autonomous code review daemon.

Watches for PRs with 'status:reviewing' label and automatically
reviews them using the configured LLM backend (codex or claude).
"""

import argparse
import logging
import re
import sys
import time
from pathlib import Path

# Add parent directory to path for shared imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.config import get_agent_config
from shared.github_client import GitHubClient, PullRequest
from shared.llm_client import LLMClient
from shared.lock import LockManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("reviewer-agent")


class ReviewerAgent:
    """Autonomous reviewer that reviews PRs."""

    # Status labels
    STATUS_REVIEWING = "status:reviewing"
    STATUS_IN_REVIEW = "status:in-review"
    STATUS_APPROVED = "status:approved"
    STATUS_CHANGES_REQUESTED = "status:changes-requested"

    def __init__(self, repo: str, config_path: str | None = None):
        self.repo = repo
        self.config = get_agent_config(repo, config_path)

        # Initialize components
        self.github = GitHubClient(repo, gh_cli=self.config.gh_cli)
        self.lock = LockManager(self.github, agent_type="reviewer")
        self.llm = LLMClient(self.config)

        logger.info(f"Reviewer Agent initialized for {repo}")
        logger.info(f"LLM backend: {self.config.llm_backend}")

    def run(self) -> None:
        """Main daemon loop."""
        logger.info(f"Starting Reviewer Agent daemon for {self.repo}")
        logger.info(f"Poll interval: {self.config.poll_interval}s")

        while True:
            try:
                self._process_reviewing_prs()
                time.sleep(self.config.poll_interval)

            except KeyboardInterrupt:
                logger.info("Shutting down Reviewer Agent")
                break
            except Exception as e:
                logger.exception(f"Unexpected error: {e}")
                time.sleep(60)

    def run_once(self) -> bool:
        """Process one PR and return. For testing."""
        prs = self.github.list_prs(labels=[self.STATUS_REVIEWING])
        if not prs:
            logger.info("No PRs to review")
            return False

        return self._try_review_pr(prs[0])

    def _process_reviewing_prs(self) -> None:
        """Find and process PRs ready for review."""
        prs = self.github.list_prs(labels=[self.STATUS_REVIEWING])

        if not prs:
            logger.debug("No PRs to review")
            return

        logger.info(f"Found {len(prs)} PR(s) to review")

        for pr in prs:
            # Check if CI has passed
            if not self.github.is_ci_green(pr.number):
                logger.debug(f"PR #{pr.number} CI not green, skipping")
                continue

            if self._try_review_pr(pr):
                logger.info(f"Successfully reviewed PR #{pr.number}")

    def _try_review_pr(self, pr: PullRequest) -> bool:
        """
        Try to review a single PR.

        Returns True if reviewed successfully.
        """
        logger.info(f"Attempting to review PR #{pr.number}: {pr.title}")

        # Try to acquire lock
        lock_result = self.lock.try_lock_pr(
            pr.number,
            self.STATUS_REVIEWING,
            self.STATUS_IN_REVIEW,
        )

        if not lock_result.success:
            logger.debug(f"Could not lock PR #{pr.number}: {lock_result.error}")
            return False

        try:
            # Get the spec from linked issue
            spec = self._get_linked_spec(pr)

            # Get the diff
            diff = self.github.get_pr_diff(pr.number)
            if not diff:
                raise RuntimeError("Failed to get PR diff")

            # Review with LLM
            logger.info(f"Reviewing with {self.config.llm_backend}...")
            review_result = self._review_code(spec, diff)

            if review_result["approved"]:
                logger.info(f"PR #{pr.number} approved")
                self.github.approve_pr(pr.number, review_result["comment"])
                self.github.remove_pr_label(pr.number, self.STATUS_IN_REVIEW)
                self.github.add_pr_label(pr.number, self.STATUS_APPROVED)

                # Auto-merge if configured
                if self.config.auto_merge:
                    logger.info(f"Auto-merging PR #{pr.number} with method: {self.config.merge_method}")
                    if self.github.merge_pr(pr.number, method=self.config.merge_method):
                        logger.info(f"Successfully merged PR #{pr.number}")
                    else:
                        logger.error(f"Failed to auto-merge PR #{pr.number}")
                        self.github.comment_pr(
                            pr.number,
                            "⚠️ Auto-merge failed. Please merge manually."
                        )
            else:
                logger.info(f"PR #{pr.number} needs changes")
                self.github.request_changes_pr(pr.number, review_result["comment"])
                self.github.remove_pr_label(pr.number, self.STATUS_IN_REVIEW)
                self.github.add_pr_label(pr.number, self.STATUS_CHANGES_REQUESTED)

            return True

        except Exception as e:
            logger.error(f"Failed to review PR #{pr.number}: {e}")

            # Reset status
            self.github.remove_pr_label(pr.number, self.STATUS_IN_REVIEW)
            self.github.add_pr_label(pr.number, self.STATUS_REVIEWING)

            self.github.comment_pr(
                pr.number,
                f"⚠️ Auto-review failed: {e}\n\nPlease review manually.",
            )

            return False

    def _get_linked_spec(self, pr: PullRequest) -> str:
        """Extract spec from linked issue."""
        # Look for "Closes #123" or "Fixes #123" in PR body
        patterns = [
            r"[Cc]loses?\s+#(\d+)",
            r"[Ff]ixes?\s+#(\d+)",
            r"[Rr]esolves?\s+#(\d+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, pr.body)
            if match:
                issue_number = int(match.group(1))
                issue = self.github.get_issue(issue_number)
                if issue:
                    return issue.body

        # Fallback: use PR body as spec
        logger.warning("No linked issue found, using PR body as spec")
        return pr.body

    def _review_code(self, spec: str, diff: str) -> dict:
        """
        Review code using LLM.

        Returns dict with 'approved' bool and 'comment' string.
        """
        # Truncate very large diffs
        truncated_diff = diff[:50000]

        result = self.llm.review_code(spec, truncated_diff)

        if not result.success:
            logger.error(f"LLM review failed: {result.error}")
            return {
                "approved": False,
                "comment": f"Auto-review encountered an error: {result.error}\n\nPlease review manually.",
            }

        output = result.output

        # Parse decision - look for DECISION line or ### Decision header
        approved = False
        lines = output.split("\n")
        for i, line in enumerate(lines):
            line_upper = line.strip().upper()

            # Format 1: "DECISION: APPROVE"
            if line_upper.startswith("DECISION:"):
                decision_value = line_upper.replace("DECISION:", "").strip()
                approved = decision_value == "APPROVE"
                break

            # Format 2: "### Decision" followed by "APPROVE" on next line
            if "DECISION" in line_upper and i + 1 < len(lines):
                next_line = lines[i + 1].strip().upper()
                if next_line == "APPROVE":
                    approved = True
                    break
                elif next_line == "CHANGES_REQUESTED":
                    approved = False
                    break

        # Clean up output for comment
        comment = (
            f"## Auto-Review by Reviewer Agent ({self.config.llm_backend})\n\n{output}"
        )

        return {"approved": approved, "comment": comment}


def main():
    parser = argparse.ArgumentParser(
        description="Reviewer Agent - Autonomous code review daemon"
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
        help="Review one PR and exit",
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

    agent = ReviewerAgent(args.repo, config_path=args.config)

    if args.once:
        success = agent.run_once()
        sys.exit(0 if success else 1)
    else:
        agent.run()


if __name__ == "__main__":
    main()
