"""Tests for orchestrator MonitorService anomaly detection."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from orchestrator.monitor import (
    CI_LOOP_COUNT,
    FAILURE_LOOP_COUNT,
    SPEC_BLOAT_RATIO,
    STALE_IMPLEMENTING_MINUTES,
    AnomalyType,
    MonitorService,
    RepoSnapshot,
)
from shared.github_client import Issue, PullRequest


def _make_issue(number: int, labels: list[str], body: str = "spec") -> Issue:
    return Issue(number=number, title=f"Issue #{number}", body=body, labels=labels)


def _make_pr(number: int) -> PullRequest:
    return PullRequest(
        number=number,
        title=f"PR #{number}",
        body="",
        labels=[],
        head_ref="auto/issue-1",
        base_ref="main",
    )


def _make_snapshot(
    implementing: list[Issue] | None = None,
    failed: list[Issue] | None = None,
    ci_failed: list[Issue] | None = None,
    open_prs: list[PullRequest] | None = None,
) -> RepoSnapshot:
    issues = (implementing or []) + (failed or []) + (ci_failed or [])
    return RepoSnapshot(
        implementing_issues=implementing or [],
        failed_issues=failed or [],
        ci_failed_issues=ci_failed or [],
        open_prs=open_prs or [],
        issue_bodies={i.number: i.body for i in issues},
    )


@pytest.fixture
def github() -> MagicMock:
    mock = MagicMock()
    mock.get_issue_comments.return_value = []
    return mock


@pytest.fixture
def monitor(github: MagicMock) -> MonitorService:
    return MonitorService(github)


# ── FAILURE_LOOP ──────────────────────────────────────────────────────────────


class TestFailureLoop:
    def test_detects_failure_loop(
        self, monitor: MonitorService, github: MagicMock
    ) -> None:
        github.get_issue_comments.return_value = [
            {"body": "Processing failed"} for _ in range(FAILURE_LOOP_COUNT)
        ]
        issue = _make_issue(10, ["status:failed"])
        snapshot = _make_snapshot(failed=[issue])

        anomalies = monitor.detect_anomalies(snapshot)

        assert len(anomalies) == 1
        assert anomalies[0].anomaly_type == AnomalyType.FAILURE_LOOP
        assert anomalies[0].issue_number == 10

    def test_no_failure_loop_below_threshold(
        self, monitor: MonitorService, github: MagicMock
    ) -> None:
        github.get_issue_comments.return_value = [
            {"body": "Processing failed"} for _ in range(FAILURE_LOOP_COUNT - 1)
        ]
        issue = _make_issue(10, ["status:failed"])
        snapshot = _make_snapshot(failed=[issue])

        anomalies = monitor.detect_anomalies(snapshot)

        assert not any(a.anomaly_type == AnomalyType.FAILURE_LOOP for a in anomalies)


# ── STALE_IMPLEMENTING ────────────────────────────────────────────────────────


class TestStaleImplementing:
    def test_detects_stale_implementing(self, monitor: MonitorService) -> None:
        issue = _make_issue(20, ["status:implementing"])
        # Backdate the first-seen timestamp
        past = datetime.now(UTC) - timedelta(minutes=STALE_IMPLEMENTING_MINUTES + 5)
        monitor._implementing_since[20] = past

        snapshot = _make_snapshot(implementing=[issue])
        anomalies = monitor.detect_anomalies(snapshot)

        assert any(
            a.anomaly_type == AnomalyType.STALE_IMPLEMENTING and a.issue_number == 20
            for a in anomalies
        )

    def test_no_stale_when_recent(self, monitor: MonitorService) -> None:
        issue = _make_issue(20, ["status:implementing"])
        monitor._implementing_since[20] = datetime.now(UTC)

        snapshot = _make_snapshot(implementing=[issue])
        anomalies = monitor.detect_anomalies(snapshot)

        assert not any(
            a.anomaly_type == AnomalyType.STALE_IMPLEMENTING for a in anomalies
        )

    def test_implementing_since_cleared_when_no_longer_implementing(
        self, monitor: MonitorService
    ) -> None:
        monitor._implementing_since[20] = datetime.now(UTC)
        snapshot = _make_snapshot(implementing=[])  # issue 20 no longer implementing

        monitor.detect_anomalies(snapshot)

        assert 20 not in monitor._implementing_since


# ── SPEC_BLOAT ────────────────────────────────────────────────────────────────


class TestSpecBloat:
    def test_detects_spec_bloat(self, monitor: MonitorService) -> None:
        short_body = "x" * 100
        bloated_body = "x" * int(100 * SPEC_BLOAT_RATIO + 1)

        prev_issue = _make_issue(30, ["status:failed"], body=short_body)
        curr_issue = _make_issue(30, ["status:failed"], body=bloated_body)

        prev_snapshot = _make_snapshot(failed=[prev_issue])
        curr_snapshot = _make_snapshot(failed=[curr_issue])

        monitor.detect_anomalies(prev_snapshot)  # sets _previous
        anomalies = monitor.detect_anomalies(curr_snapshot)

        assert any(
            a.anomaly_type == AnomalyType.SPEC_BLOAT and a.issue_number == 30
            for a in anomalies
        )

    def test_no_bloat_below_ratio(self, monitor: MonitorService) -> None:
        prev_issue = _make_issue(30, ["status:failed"], body="x" * 100)
        curr_issue = _make_issue(30, ["status:failed"], body="x" * 200)  # 2x, below 3x

        monitor.detect_anomalies(_make_snapshot(failed=[prev_issue]))
        anomalies = monitor.detect_anomalies(_make_snapshot(failed=[curr_issue]))

        assert not any(a.anomaly_type == AnomalyType.SPEC_BLOAT for a in anomalies)

    def test_no_bloat_on_first_snapshot(self, monitor: MonitorService) -> None:
        issue = _make_issue(30, ["status:failed"], body="x" * 1000)
        anomalies = monitor.detect_anomalies(_make_snapshot(failed=[issue]))

        assert not any(a.anomaly_type == AnomalyType.SPEC_BLOAT for a in anomalies)


# ── CI_LOOP ───────────────────────────────────────────────────────────────────


class TestCILoop:
    def test_detects_ci_loop(self, monitor: MonitorService, github: MagicMock) -> None:
        github.get_issue_comments.return_value = [
            {"body": "CI fix attempt"} for _ in range(CI_LOOP_COUNT)
        ]
        pr = _make_pr(5)
        snapshot = _make_snapshot(open_prs=[pr])

        anomalies = monitor.detect_anomalies(snapshot)

        assert any(
            a.anomaly_type == AnomalyType.CI_LOOP and a.pr_number == 5
            for a in anomalies
        )

    def test_no_ci_loop_below_threshold(
        self, monitor: MonitorService, github: MagicMock
    ) -> None:
        github.get_issue_comments.return_value = [
            {"body": "CI fix attempt"} for _ in range(CI_LOOP_COUNT - 1)
        ]
        pr = _make_pr(5)
        snapshot = _make_snapshot(open_prs=[pr])

        anomalies = monitor.detect_anomalies(snapshot)

        assert not any(a.anomaly_type == AnomalyType.CI_LOOP for a in anomalies)


# ── AGENT_CRASH ───────────────────────────────────────────────────────────────


class TestAgentCrash:
    def test_detects_agent_crash(self, monitor: MonitorService) -> None:
        snapshot = _make_snapshot()
        anomalies = monitor.detect_anomalies(snapshot, agent_crashes=["worker"])

        assert len(anomalies) == 1
        assert anomalies[0].anomaly_type == AnomalyType.AGENT_CRASH
        assert "worker" in anomalies[0].detail

    def test_no_crash_anomaly_when_empty(self, monitor: MonitorService) -> None:
        snapshot = _make_snapshot()
        anomalies = monitor.detect_anomalies(snapshot, agent_crashes=[])

        assert not any(a.anomaly_type == AnomalyType.AGENT_CRASH for a in anomalies)


# ── take_snapshot ─────────────────────────────────────────────────────────────


class TestTakeSnapshot:
    def test_take_snapshot_populates_fields(
        self, monitor: MonitorService, github: MagicMock
    ) -> None:
        implementing = [_make_issue(1, ["status:implementing"])]
        failed = [_make_issue(2, ["status:failed"])]
        github.list_issues.side_effect = lambda labels, **_: (
            implementing
            if "status:implementing" in labels
            else failed
            if "status:failed" in labels
            else []
        )
        github.list_prs.return_value = [_make_pr(10)]

        snapshot = monitor.take_snapshot()

        assert snapshot.implementing_issues == implementing
        assert snapshot.failed_issues == failed
        assert snapshot.open_prs == [_make_pr(10)]
        assert 1 in snapshot.issue_bodies
        assert 2 in snapshot.issue_bodies

    def test_implementing_since_set_on_first_seen(
        self, monitor: MonitorService, github: MagicMock
    ) -> None:
        issue = _make_issue(5, ["status:implementing"])
        github.list_issues.side_effect = lambda labels, **_: (
            [issue] if "status:implementing" in labels else []
        )
        github.list_prs.return_value = []

        monitor.take_snapshot()

        assert 5 in monitor._implementing_since
