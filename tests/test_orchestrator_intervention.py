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


def _mock_claude(text: str) -> MagicMock:
    block = MagicMock()
    block.text = text
    msg = MagicMock()
    msg.content = [block]
    return msg


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
    with patch("orchestrator.intervention.anthropic.Anthropic"):
        svc = InterventionService(github)
    return svc


class TestDecide:
    def test_returns_plan_from_claude(self, service: InterventionService) -> None:
        service.client.messages.create.return_value = _mock_claude(
            json.dumps({"action": "ignore", "reason": "transient", "details": ""})
        )
        plan = service.decide(_make_anomaly(AnomalyType.FAILURE_LOOP))

        assert plan.action == InterventionAction.IGNORE
        assert plan.reason == "transient"

    def test_defaults_to_mark_manual_on_bad_json(
        self, service: InterventionService
    ) -> None:
        service.client.messages.create.return_value = _mock_claude("not json")

        plan = service.decide(_make_anomaly(AnomalyType.SPEC_BLOAT))

        assert plan.action == InterventionAction.MARK_MANUAL

    def test_generates_new_spec_for_reset_spec(
        self, service: InterventionService
    ) -> None:
        service.client.messages.create.side_effect = [
            _mock_claude(
                json.dumps({"action": "reset_spec", "reason": "bloated", "details": ""})
            ),
            _mock_claude("## Minimal Spec\n- Do one thing"),
        ]
        plan = service.decide(_make_anomaly(AnomalyType.SPEC_BLOAT))

        assert plan.action == InterventionAction.RESET_SPEC
        assert plan.new_spec == "## Minimal Spec\n- Do one thing"

    def test_parses_json_from_code_block(self, service: InterventionService) -> None:
        wrapped = '```json\n{"action": "ignore", "reason": "ok", "details": ""}\n```'
        service.client.messages.create.return_value = _mock_claude(wrapped)

        plan = service.decide(_make_anomaly(AnomalyType.AGENT_CRASH, issue_number=None))

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
