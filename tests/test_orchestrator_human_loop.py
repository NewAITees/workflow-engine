"""Tests for HumanLoopService and show_decisions."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.human_loop import LABEL_HUMAN_REVIEW, LABEL_PAUSED, HumanLoopService
from orchestrator.intervention import (
    InterventionAction,
    InterventionPlan,
    InterventionService,
)
from orchestrator.monitor import Anomaly, AnomalyType
from shared.github_client import Issue


def _make_issue(number: int, labels: list[str]) -> Issue:
    return Issue(number=number, title=f"Issue #{number}", body="", labels=labels)


def _make_anomaly(issue_number: int = 10) -> Anomaly:
    return Anomaly(
        anomaly_type=AnomalyType.FAILURE_LOOP,
        detail="test",
        issue_number=issue_number,
    )


@pytest.fixture
def github() -> MagicMock:
    mock = MagicMock()
    mock.list_issues.return_value = []
    mock.comment_issue.return_value = True
    mock.add_label.return_value = True
    mock.remove_label.return_value = True
    return mock


@pytest.fixture
def service(github: MagicMock) -> HumanLoopService:
    return HumanLoopService(github)


# ── is_paused ─────────────────────────────────────────────────────────────────


class TestIsPaused:
    def test_not_paused_initially(self, service: HumanLoopService) -> None:
        assert service.is_paused(42) is False

    def test_paused_after_sync_with_paused_label(
        self, service: HumanLoopService, github: MagicMock
    ) -> None:
        github.list_issues.side_effect = lambda labels, **_: (
            [_make_issue(10, [LABEL_PAUSED])] if LABEL_PAUSED in labels else []
        )
        service.sync()
        assert service.is_paused(10) is True

    def test_paused_after_sync_with_human_review_label(
        self, service: HumanLoopService, github: MagicMock
    ) -> None:
        github.list_issues.side_effect = lambda labels, **_: (
            [_make_issue(20, [LABEL_HUMAN_REVIEW])]
            if LABEL_HUMAN_REVIEW in labels
            else []
        )
        service.sync()
        assert service.is_paused(20) is True

    def test_not_paused_after_labels_removed(
        self, service: HumanLoopService, github: MagicMock
    ) -> None:
        # First sync: issue 10 has human-review
        github.list_issues.side_effect = lambda labels, **_: (
            [_make_issue(10, [LABEL_HUMAN_REVIEW])]
            if LABEL_HUMAN_REVIEW in labels
            else []
        )
        service.sync()
        assert service.is_paused(10) is True

        # Second sync: label removed
        github.list_issues.return_value = []
        github.list_issues.side_effect = None
        service.sync()
        assert service.is_paused(10) is False


# ── sync() resume detection ───────────────────────────────────────────────────


class TestSyncResume:
    def test_returns_resolved_issue_numbers(
        self, service: HumanLoopService, github: MagicMock
    ) -> None:
        # First sync: issues 10 and 11 need human review
        github.list_issues.side_effect = lambda labels, **_: (
            [
                _make_issue(10, [LABEL_HUMAN_REVIEW]),
                _make_issue(11, [LABEL_HUMAN_REVIEW]),
            ]
            if LABEL_HUMAN_REVIEW in labels
            else []
        )
        service.sync()

        # Second sync: only issue 11 remains
        github.list_issues.side_effect = lambda labels, **_: (
            [_make_issue(11, [LABEL_HUMAN_REVIEW])]
            if LABEL_HUMAN_REVIEW in labels
            else []
        )
        resolved = service.sync()

        assert resolved == [10]

    def test_posts_resume_comment_on_resolved_issue(
        self, service: HumanLoopService, github: MagicMock
    ) -> None:
        github.list_issues.side_effect = lambda labels, **_: (
            [_make_issue(10, [LABEL_HUMAN_REVIEW])]
            if LABEL_HUMAN_REVIEW in labels
            else []
        )
        service.sync()

        github.list_issues.return_value = []
        github.list_issues.side_effect = None
        service.sync()

        github.comment_issue.assert_called_once()
        call_args = github.comment_issue.call_args[0]
        assert call_args[0] == 10
        assert "resum" in call_args[1].lower()

    def test_no_resolved_on_first_sync(
        self, service: HumanLoopService, github: MagicMock
    ) -> None:
        github.list_issues.side_effect = lambda labels, **_: (
            [_make_issue(10, [LABEL_HUMAN_REVIEW])]
            if LABEL_HUMAN_REVIEW in labels
            else []
        )
        resolved = service.sync()
        assert resolved == []

    def test_paused_count(self, service: HumanLoopService, github: MagicMock) -> None:
        github.list_issues.side_effect = lambda labels, **_: (
            [_make_issue(1, [LABEL_PAUSED])]
            if LABEL_PAUSED in labels
            else [_make_issue(2, [LABEL_HUMAN_REVIEW])]
            if LABEL_HUMAN_REVIEW in labels
            else []
        )
        service.sync()
        assert service.paused_count() == 2


# ── show_decisions ────────────────────────────────────────────────────────────


class TestShowDecisions:
    def _make_service(self) -> InterventionService:
        with patch("orchestrator.intervention.anthropic.Anthropic"):
            svc = InterventionService(MagicMock())
        return svc

    def test_empty_log_message(self) -> None:
        svc = self._make_service()
        assert "No intervention" in svc.show_decisions()

    def test_formats_log_entries(self) -> None:
        svc = self._make_service()
        plan = InterventionPlan(
            action=InterventionAction.RESET_SPEC,
            reason="spec bloated",
            details="",
            anomaly=_make_anomaly(issue_number=42),
            decided_at=datetime(2026, 3, 16, 10, 30, tzinfo=UTC),
        )
        svc._decision_log.append(plan)
        output = svc.show_decisions()

        assert "reset_spec" in output
        assert "issue#42" in output
        assert "spec bloated" in output
        assert "2026-03-16" in output

    def test_multiple_entries_all_appear(self) -> None:
        svc = self._make_service()
        for i, action in enumerate(
            [InterventionAction.IGNORE, InterventionAction.MARK_MANUAL]
        ):
            svc._decision_log.append(
                InterventionPlan(
                    action=action,
                    reason=f"reason-{i}",
                    details="",
                    anomaly=_make_anomaly(issue_number=i + 1),
                )
            )
        output = svc.show_decisions()
        assert "ignore" in output
        assert "mark_manual" in output
