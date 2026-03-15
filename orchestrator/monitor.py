"""GitHub state monitoring and anomaly detection for the Orchestrator."""

import logging
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.github_client import GitHubClient, Issue, PullRequest

logger = logging.getLogger(__name__)

# Thresholds
FAILURE_LOOP_COUNT = 3  # failures before FAILURE_LOOP anomaly
STALE_IMPLEMENTING_MINUTES = 30  # minutes before STALE_IMPLEMENTING anomaly
SPEC_BLOAT_RATIO = 3.0  # body growth ratio before SPEC_BLOAT anomaly
CI_LOOP_COUNT = 3  # CI fix attempts before CI_LOOP anomaly


class AnomalyType(Enum):
    FAILURE_LOOP = "failure_loop"
    STALE_IMPLEMENTING = "stale_implementing"
    SPEC_BLOAT = "spec_bloat"
    AGENT_CRASH = "agent_crash"
    CI_LOOP = "ci_loop"


@dataclass
class Anomaly:
    """A detected anomaly requiring potential intervention."""

    anomaly_type: AnomalyType
    detail: str
    issue_number: int | None = None
    pr_number: int | None = None
    detected_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __str__(self) -> str:
        target = (
            f"issue#{self.issue_number}"
            if self.issue_number
            else f"pr#{self.pr_number}"
            if self.pr_number
            else "system"
        )
        return f"[{self.anomaly_type.value}] {target}: {self.detail}"


@dataclass
class RepoSnapshot:
    """Point-in-time snapshot of GitHub repository state."""

    implementing_issues: list[Issue]
    failed_issues: list[Issue]
    ci_failed_issues: list[Issue]
    open_prs: list[PullRequest]
    issue_bodies: dict[int, str]  # issue_number → body text
    captured_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class MonitorService:
    """
    Monitors GitHub repository state and detects anomalies.

    Keeps two consecutive snapshots to detect changes over time.
    Tracks implementing_since separately to detect stale locks.
    """

    def __init__(self, github: GitHubClient):
        self.github = github
        self._previous: RepoSnapshot | None = None
        # Track when each issue first appeared as status:implementing
        self._implementing_since: dict[int, datetime] = {}

    def take_snapshot(self) -> RepoSnapshot:
        """Fetch current GitHub state and return a snapshot."""
        implementing = self.github.list_issues(
            labels=["status:implementing"], state="open", limit=50
        )
        failed = self.github.list_issues(
            labels=["status:failed"], state="open", limit=50
        )
        ci_failed = self.github.list_issues(
            labels=["status:ci-failed"], state="open", limit=50
        )
        open_prs = self.github.list_prs(state="open", limit=50)

        issue_bodies = {
            issue.number: issue.body for issue in implementing + failed + ci_failed
        }

        snapshot = RepoSnapshot(
            implementing_issues=implementing,
            failed_issues=failed,
            ci_failed_issues=ci_failed,
            open_prs=open_prs,
            issue_bodies=issue_bodies,
        )

        # Update implementing_since tracking
        current_implementing_ids = {i.number for i in implementing}
        now = datetime.now(UTC)

        for issue_num in current_implementing_ids:
            if issue_num not in self._implementing_since:
                self._implementing_since[issue_num] = now

        # Remove issues no longer implementing
        stale_keys = set(self._implementing_since) - current_implementing_ids
        for key in stale_keys:
            del self._implementing_since[key]

        return snapshot

    def detect_anomalies(
        self,
        current: RepoSnapshot,
        agent_crashes: list[str] | None = None,
    ) -> list[Anomaly]:
        """
        Detect anomalies from the current snapshot and previous snapshot.

        Args:
            current: The freshly-taken snapshot.
            agent_crashes: List of agent names that crashed since last check.

        Returns:
            List of detected anomalies (may be empty).
        """
        # Sync implementing_since with the current snapshot
        current_implementing_ids = {i.number for i in current.implementing_issues}
        stale_keys = set(self._implementing_since) - current_implementing_ids
        for key in stale_keys:
            del self._implementing_since[key]

        anomalies: list[Anomaly] = []

        anomalies.extend(self._detect_failure_loops(current))
        anomalies.extend(self._detect_stale_implementing())
        anomalies.extend(self._detect_spec_bloat(current))
        anomalies.extend(self._detect_ci_loops(current))

        for agent_name in agent_crashes or []:
            anomalies.append(
                Anomaly(
                    anomaly_type=AnomalyType.AGENT_CRASH,
                    detail=f"Agent '{agent_name}' crashed and was restarted",
                )
            )

        self._previous = current

        if anomalies:
            for a in anomalies:
                logger.warning(f"Anomaly detected: {a}")
        else:
            logger.debug("No anomalies detected")

        return anomalies

    # ── Private detection methods ─────────────────────────────────────

    def _detect_failure_loops(self, current: RepoSnapshot) -> list[Anomaly]:
        """FAILURE_LOOP: issue has 3+ 'Processing failed' comments."""
        anomalies = []
        for issue in current.failed_issues:
            comments = self.github.get_issue_comments(issue.number, limit=50)
            failure_count = sum(
                1 for c in comments if "Processing failed" in str(c.get("body", ""))
            )
            if failure_count >= FAILURE_LOOP_COUNT:
                anomalies.append(
                    Anomaly(
                        anomaly_type=AnomalyType.FAILURE_LOOP,
                        issue_number=issue.number,
                        detail=(
                            f"Issue #{issue.number} has failed {failure_count} times"
                        ),
                    )
                )
        return anomalies

    def _detect_stale_implementing(self) -> list[Anomaly]:
        """STALE_IMPLEMENTING: issue stuck in status:implementing for 30+ minutes."""
        anomalies = []
        threshold = timedelta(minutes=STALE_IMPLEMENTING_MINUTES)
        now = datetime.now(UTC)

        for issue_num, since in self._implementing_since.items():
            elapsed = now - since
            if elapsed >= threshold:
                anomalies.append(
                    Anomaly(
                        anomaly_type=AnomalyType.STALE_IMPLEMENTING,
                        issue_number=issue_num,
                        detail=(
                            f"Issue #{issue_num} has been implementing for "
                            f"{int(elapsed.total_seconds() // 60)} minutes"
                        ),
                    )
                )
        return anomalies

    def _detect_spec_bloat(self, current: RepoSnapshot) -> list[Anomaly]:
        """SPEC_BLOAT: issue body grew 3x or more since last snapshot."""
        if self._previous is None:
            return []

        anomalies = []
        for issue_num, current_body in current.issue_bodies.items():
            previous_body = self._previous.issue_bodies.get(issue_num)
            if previous_body is None:
                continue
            prev_len = len(previous_body)
            curr_len = len(current_body)
            if prev_len > 0 and curr_len >= prev_len * SPEC_BLOAT_RATIO:
                anomalies.append(
                    Anomaly(
                        anomaly_type=AnomalyType.SPEC_BLOAT,
                        issue_number=issue_num,
                        detail=(
                            f"Issue #{issue_num} body grew from {prev_len} "
                            f"to {curr_len} chars "
                            f"({curr_len / prev_len:.1f}x)"
                        ),
                    )
                )
        return anomalies

    def _detect_ci_loops(self, current: RepoSnapshot) -> list[Anomaly]:
        """CI_LOOP: PR has 3+ CI fix attempt comments."""
        anomalies = []
        for pr in current.open_prs:
            comments = self.github.get_issue_comments(pr.number, limit=50)
            ci_fix_count = sum(
                1
                for c in comments
                if "CI fix attempt" in str(c.get("body", ""))
                or "Attempting CI fix" in str(c.get("body", ""))
            )
            if ci_fix_count >= CI_LOOP_COUNT:
                anomalies.append(
                    Anomaly(
                        anomaly_type=AnomalyType.CI_LOOP,
                        pr_number=pr.number,
                        detail=(
                            f"PR #{pr.number} has had {ci_fix_count} CI fix attempts"
                        ),
                    )
                )
        return anomalies
