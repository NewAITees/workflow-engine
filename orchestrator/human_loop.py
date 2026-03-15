"""Human-in-the-loop monitoring for the Orchestrator.

Watches for issues/PRs tagged with human-review or orchestrator-paused.
When a human removes the human-review label, automation is resumed.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.github_client import GitHubClient

logger = logging.getLogger(__name__)

LABEL_HUMAN_REVIEW = "human-review"
LABEL_PAUSED = "orchestrator-paused"


class HumanLoopService:
    """
    Tracks issues/PRs paused for human attention and resumes them
    when the human-review label is removed.

    Paused issue set is rebuilt on every sync() call so it stays
    consistent with GitHub state even after manual edits.
    """

    def __init__(self, github: GitHubClient):
        self.github = github
        self._paused: set[int] = set()  # orchestrator-paused issues
        self._human_review: set[int] = set()  # human-review issues

    # ── Public API ────────────────────────────────────────────────────────────

    def sync(self) -> list[int]:
        """
        Refresh paused/human-review sets from GitHub.

        Returns a list of issue numbers where human-review was just
        resolved (label removed by human) — these are ready to resume.
        """
        current_paused = self._fetch_label_issue_nums(LABEL_PAUSED)
        current_human_review = self._fetch_label_issue_nums(LABEL_HUMAN_REVIEW)

        # Issues that had human-review before but no longer do → resolved
        resolved = self._human_review - current_human_review

        self._paused = current_paused
        self._human_review = current_human_review

        for issue_num in resolved:
            logger.info(f"Human review resolved for issue #{issue_num}, resuming")
            self._resume_issue(issue_num)

        return list(resolved)

    def is_paused(self, issue_num: int) -> bool:
        """Return True if the issue should be skipped by the Orchestrator."""
        return issue_num in self._paused or issue_num in self._human_review

    def paused_count(self) -> int:
        return len(self._paused | self._human_review)

    # ── Private ───────────────────────────────────────────────────────────────

    def _fetch_label_issue_nums(self, label: str) -> set[int]:
        issues = self.github.list_issues(labels=[label], state="open", limit=100)
        return {i.number for i in issues}

    def _resume_issue(self, issue_num: int) -> None:
        """Post a resume comment on the issue."""
        self.github.comment_issue(
            issue_num,
            (
                "✅ **Orchestrator resuming automation**\n\n"
                "`human-review` label removed — issue is back in the automation queue."
            ),
        )
