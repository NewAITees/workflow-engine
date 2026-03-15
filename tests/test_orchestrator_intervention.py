"""Tests for orchestrator InterventionService."""

import json
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.intervention import (
    InterventionAction,
    InterventionPlan,
    InterventionService,
)
from orchestrator.monitor import Anomaly, AnomalyType
from shared.github_client import Issue


def _make_anomaly(
    anomaly_type: AnomalyType,
    issue_number: int | None = 10,
    pr_number: int | None = None,
    detail: str = "test anomaly",
) -> Anomaly:
    return Anomaly(
        anomaly_type=anomaly_type,
        detail=detail,
        issue_number=issue_number,
        pr_number=pr_number,
    )


def _make_issue(
    number: int = 10, title: str = "Test issue", body: str = "spec"
) -> Issue:
    return Issue(number=number, title=title, body=body, labels=[])


@pytest.fixture
def github() -> MagicMock:
    mock = MagicMock()
    mock.get_issue.return_value = _make_issue()
    mock.update_issue_body.return_value = True
    mock.add_label.return_value = True
    mock.remove_label.return_value = True
    mock.comment_issue.return_value = True
    return mock


@pytest.fixture
def service(github: MagicMock) -> InterventionService:
    return InterventionService(github)


class TestDecide:
    def test_returns_plan_from_claude(self, service: InterventionService) -> None:
        with patch.object(
            service,
            "_call_claude",
            return_value=json.dumps(
                {"action": "ignore", "reason": "transient", "details": ""}
            ),
        ):
            plan = service.decide(_make_anomaly(AnomalyType.FAILURE_LOOP))

        assert plan.action == InterventionAction.IGNORE
        assert plan.reason == "transient"

    def test_defaults_to_mark_manual_on_bad_json(
        self, service: InterventionService
    ) -> None:
        with patch.object(service, "_call_claude", return_value="not json"):
            plan = service.decide(_make_anomaly(AnomalyType.SPEC_BLOAT))

        assert plan.action == InterventionAction.MARK_MANUAL

    def test_generates_new_spec_for_reset_spec(
        self, service: InterventionService
    ) -> None:
        responses = iter(
            [
                json.dumps(
                    {"action": "reset_spec", "reason": "bloated", "details": ""}
                ),
                "## Minimal Spec\n- Do one thing",
            ]
        )
        with patch.object(
            service, "_call_claude", side_effect=lambda *_: next(responses)
        ):
            plan = service.decide(_make_anomaly(AnomalyType.SPEC_BLOAT))

        assert plan.action == InterventionAction.RESET_SPEC
        assert plan.new_spec == "## Minimal Spec\n- Do one thing"

    def test_parses_json_from_code_block(self, service: InterventionService) -> None:
        wrapped = '```json\n{"action": "ignore", "reason": "ok", "details": ""}\n```'
        with patch.object(service, "_call_claude", return_value=wrapped):
            plan = service.decide(
                _make_anomaly(AnomalyType.AGENT_CRASH, issue_number=None)
            )

        assert plan.action == InterventionAction.IGNORE


class TestExecuteResetSpec:
    def test_updates_body_and_transitions_to_ready(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        plan = InterventionPlan(
            action=InterventionAction.RESET_SPEC,
            reason="bloated",
            details="",
            anomaly=_make_anomaly(AnomalyType.SPEC_BLOAT, issue_number=10),
            new_spec="## Minimal spec",
        )

        assert service.execute(plan) is True
        github.update_issue_body.assert_called_once_with(10, "## Minimal spec")
        github.add_label.assert_called_with(10, "status:ready")
        github.comment_issue.assert_called_once()

    def test_fails_without_new_spec(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        plan = InterventionPlan(
            action=InterventionAction.RESET_SPEC,
            reason="bloated",
            details="",
            anomaly=_make_anomaly(AnomalyType.SPEC_BLOAT, issue_number=10),
            new_spec=None,
        )

        assert service.execute(plan) is False
        github.update_issue_body.assert_not_called()

    def test_increments_intervention_count(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        plan = InterventionPlan(
            action=InterventionAction.RESET_SPEC,
            reason="r",
            details="",
            anomaly=_make_anomaly(AnomalyType.SPEC_BLOAT, issue_number=10),
            new_spec="new spec",
        )
        service.execute(plan)

        assert service.intervention_count(10) == 1


class TestExecuteStopWorker:
    def test_adds_paused_label(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        plan = InterventionPlan(
            action=InterventionAction.STOP_WORKER,
            reason="resource hog",
            details="",
            anomaly=_make_anomaly(AnomalyType.STALE_IMPLEMENTING, issue_number=20),
        )

        assert service.execute(plan) is True
        github.add_label.assert_called_with(20, "orchestrator-paused")
        github.comment_issue.assert_called_once()


class TestExecuteMarkManual:
    def test_adds_human_review_label(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        plan = InterventionPlan(
            action=InterventionAction.MARK_MANUAL,
            reason="needs human",
            details="",
            anomaly=_make_anomaly(AnomalyType.CI_LOOP, issue_number=None, pr_number=5),
        )

        assert service.execute(plan) is True
        github.add_label.assert_called_with(5, "human-review")
        github.comment_issue.assert_called_once()


class TestExecuteIgnore:
    def test_does_nothing(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        plan = InterventionPlan(
            action=InterventionAction.IGNORE,
            reason="false positive",
            details="",
            anomaly=_make_anomaly(AnomalyType.AGENT_CRASH, issue_number=None),
        )

        assert service.execute(plan) is True
        github.comment_issue.assert_not_called()
        github.add_label.assert_not_called()


class TestInterventionCount:
    def test_starts_at_zero(self, service: InterventionService) -> None:
        assert service.intervention_count(99) == 0

    def test_increments_per_execute(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        plan = InterventionPlan(
            action=InterventionAction.STOP_WORKER,
            reason="r",
            details="",
            anomaly=_make_anomaly(AnomalyType.STALE_IMPLEMENTING, issue_number=7),
        )
        service.execute(plan)
        service.execute(plan)

        assert service.intervention_count(7) == 2


class TestExecuteCreateIssue:
    def test_appends_footer_with_issue_source(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        """Footer with Source line when anomaly has issue_number."""
        github.create_issue.return_value = 99
        plan = InterventionPlan(
            action=InterventionAction.CREATE_ISSUE,
            reason="bug found",
            details="",
            anomaly=_make_anomaly(
                AnomalyType.FAILURE_LOOP, issue_number=10, pr_number=None
            ),
            new_issue_title="Fix bug",
            new_issue_body="## Overview\nFix it",
        )

        assert service.execute(plan) is True
        call_args = github.create_issue.call_args
        body_passed = call_args[0][1]
        assert "---" in body_passed
        assert "issue #10" in body_passed
        assert "failure_loop" in body_passed

    def test_appends_footer_with_pr_source(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        """Footer with Source line when anomaly has pr_number."""
        github.create_issue.return_value = 99
        plan = InterventionPlan(
            action=InterventionAction.CREATE_ISSUE,
            reason="ci loop",
            details="",
            anomaly=_make_anomaly(AnomalyType.CI_LOOP, issue_number=None, pr_number=5),
            new_issue_title="Fix CI",
            new_issue_body="## Overview\nFix CI",
        )

        assert service.execute(plan) is True
        body_passed = github.create_issue.call_args[0][1]
        assert "PR #5" in body_passed

    def test_footer_has_no_source_line_when_neither_set(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        """Footer omits Source line when both issue_number and pr_number are None."""
        github.create_issue.return_value = 99
        plan = InterventionPlan(
            action=InterventionAction.CREATE_ISSUE,
            reason="system",
            details="",
            anomaly=_make_anomaly(
                AnomalyType.AGENT_CRASH, issue_number=None, pr_number=None
            ),
            new_issue_title="Fix crash",
            new_issue_body="body",
        )

        assert service.execute(plan) is True
        body_passed = github.create_issue.call_args[0][1]
        assert "Source:" not in body_passed

    def test_footer_contains_timestamp_and_anomaly_type(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        """Footer always contains anomaly type and UTC ISO timestamp."""
        github.create_issue.return_value = 99
        plan = InterventionPlan(
            action=InterventionAction.CREATE_ISSUE,
            reason="r",
            details="",
            anomaly=_make_anomaly(
                AnomalyType.FAILURE_LOOP, issue_number=None, pr_number=None
            ),
            new_issue_title="title",
            new_issue_body="body",
        )

        assert service.execute(plan) is True
        body_passed = github.create_issue.call_args[0][1]
        assert "failure_loop" in body_passed
        # ISO timestamp contains "T" and "+" or "Z"
        assert "+00:00" in body_passed or "Z" in body_passed

    def test_labels_unchanged(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        """Existing label assignment (status:ready, orchestrator) is unchanged."""
        github.create_issue.return_value = 42
        plan = InterventionPlan(
            action=InterventionAction.CREATE_ISSUE,
            reason="r",
            details="",
            anomaly=_make_anomaly(
                AnomalyType.FAILURE_LOOP, issue_number=None, pr_number=None
            ),
            new_issue_title="title",
            new_issue_body="body",
        )

        service.execute(plan)
        call_kwargs = github.create_issue.call_args[1]
        assert "status:ready" in call_kwargs.get("labels", [])
        assert "orchestrator" in call_kwargs.get("labels", [])

    def test_returns_false_without_title_or_body(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        """Returns False when title or body missing."""
        plan = InterventionPlan(
            action=InterventionAction.CREATE_ISSUE,
            reason="r",
            details="",
            anomaly=_make_anomaly(AnomalyType.FAILURE_LOOP),
            new_issue_title=None,
            new_issue_body=None,
        )
        assert service.execute(plan) is False
        github.create_issue.assert_not_called()


class TestBuildFooter:
    def test_with_issue_number(self, service: InterventionService) -> None:
        """Footer includes issue source when issue_number is set."""
        anomaly = _make_anomaly(
            AnomalyType.FAILURE_LOOP, issue_number=7, pr_number=None
        )
        footer = service._build_footer(anomaly)
        assert "issue #7" in footer
        assert (
            "Source:" not in footer.split("issue #7")[0].split("\n")[-1]
            or "Source:" in footer
        )

    def test_with_pr_number(self, service: InterventionService) -> None:
        """Footer includes PR source when pr_number is set."""
        anomaly = _make_anomaly(AnomalyType.CI_LOOP, issue_number=None, pr_number=3)
        footer = service._build_footer(anomaly)
        assert "PR #3" in footer

    def test_with_both(self, service: InterventionService) -> None:
        """Footer includes both when both are set."""
        anomaly = _make_anomaly(AnomalyType.FAILURE_LOOP, issue_number=5, pr_number=2)
        footer = service._build_footer(anomaly)
        assert "issue #5" in footer
        assert "PR #2" in footer

    def test_with_neither_has_no_source_line(
        self, service: InterventionService
    ) -> None:
        """Footer omits Source line when neither issue_number nor pr_number is set."""
        anomaly = _make_anomaly(
            AnomalyType.AGENT_CRASH, issue_number=None, pr_number=None
        )
        footer = service._build_footer(anomaly)
        assert "Source:" not in footer

    def test_contains_anomaly_type_and_detail(
        self, service: InterventionService
    ) -> None:
        """Footer contains anomaly_type.value and detail."""
        anomaly = _make_anomaly(
            AnomalyType.SPEC_BLOAT,
            issue_number=None,
            pr_number=None,
            detail="spec is huge",
        )
        footer = service._build_footer(anomaly)
        assert "spec_bloat" in footer
        assert "spec is huge" in footer

    def test_contains_utc_timestamp(self, service: InterventionService) -> None:
        """Footer contains a UTC ISO timestamp."""
        anomaly = _make_anomaly(
            AnomalyType.FAILURE_LOOP, issue_number=None, pr_number=None
        )
        footer = service._build_footer(anomaly)
        assert "+00:00" in footer


class TestCallClaude:
    def test_returns_stdout_on_success(self, service: InterventionService) -> None:
        with patch("orchestrator.intervention.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="response text\n", stderr=""
            )
            result = service._call_claude("some prompt")
        assert result == "response text"

    def test_returns_empty_on_timeout(self, service: InterventionService) -> None:
        import subprocess

        with patch(
            "orchestrator.intervention.subprocess.run",
            side_effect=subprocess.TimeoutExpired("claude", 120),
        ):
            result = service._call_claude("prompt")
        assert result == ""

    def test_returns_empty_when_cli_not_found(
        self, service: InterventionService
    ) -> None:
        with patch(
            "orchestrator.intervention.subprocess.run", side_effect=FileNotFoundError
        ):
            result = service._call_claude("prompt")
        assert result == ""
