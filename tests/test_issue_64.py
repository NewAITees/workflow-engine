"""Regression tests for issue #64: anomaly footer, mypy fixes, uv PATH fix."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.intervention import (
    InterventionAction,
    InterventionPlan,
    InterventionService,
)
from orchestrator.monitor import Anomaly, AnomalyType

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_anomaly(
    anomaly_type: AnomalyType = AnomalyType.FAILURE_LOOP,
    issue_number: int | None = None,
    pr_number: int | None = None,
    detail: str = "test detail",
) -> Anomaly:
    """Create an Anomaly for testing."""
    return Anomaly(
        anomaly_type=anomaly_type,
        detail=detail,
        issue_number=issue_number,
        pr_number=pr_number,
    )


@pytest.fixture
def github() -> MagicMock:
    """Mock GitHubClient."""
    mock = MagicMock()
    mock.create_issue.return_value = 99
    mock.comment_issue.return_value = True
    mock.add_label.return_value = True
    mock.remove_label.return_value = True
    return mock


@pytest.fixture
def service(github: MagicMock) -> InterventionService:
    """InterventionService with mocked GitHub."""
    return InterventionService(github)


# ── _build_footer() ───────────────────────────────────────────────────────────


class TestBuildFooter:
    """Tests for InterventionService._build_footer()."""

    def test_footer_includes_issue_source(self, service: InterventionService) -> None:
        """Footer has Source line with issue number when issue_number is set."""
        anomaly = _make_anomaly(
            AnomalyType.FAILURE_LOOP, issue_number=10, pr_number=None
        )
        footer = service._build_footer(anomaly)
        assert "issue #10" in footer
        assert "Source:" in footer

    def test_footer_includes_pr_source(self, service: InterventionService) -> None:
        """Footer has Source line with PR number when pr_number is set."""
        anomaly = _make_anomaly(AnomalyType.CI_LOOP, issue_number=None, pr_number=5)
        footer = service._build_footer(anomaly)
        assert "PR #5" in footer
        assert "Source:" in footer

    def test_footer_includes_both_sources(self, service: InterventionService) -> None:
        """Footer includes both issue and PR when both are set."""
        anomaly = _make_anomaly(AnomalyType.FAILURE_LOOP, issue_number=7, pr_number=3)
        footer = service._build_footer(anomaly)
        assert "issue #7" in footer
        assert "PR #3" in footer

    def test_footer_omits_source_when_neither_set(
        self, service: InterventionService
    ) -> None:
        """Footer has no Source line when both issue_number and pr_number are None."""
        anomaly = _make_anomaly(
            AnomalyType.AGENT_CRASH, issue_number=None, pr_number=None
        )
        footer = service._build_footer(anomaly)
        assert "Source:" not in footer

    def test_footer_contains_anomaly_type_value(
        self, service: InterventionService
    ) -> None:
        """Footer includes anomaly_type.value."""
        anomaly = _make_anomaly(
            AnomalyType.SPEC_BLOAT, detail="spec too long", issue_number=None
        )
        footer = service._build_footer(anomaly)
        assert "spec_bloat" in footer

    def test_footer_contains_detail(self, service: InterventionService) -> None:
        """Footer includes the anomaly detail."""
        anomaly = _make_anomaly(
            AnomalyType.FAILURE_LOOP, detail="something broke", issue_number=None
        )
        footer = service._build_footer(anomaly)
        assert "something broke" in footer

    def test_footer_contains_utc_iso_timestamp(
        self, service: InterventionService
    ) -> None:
        """Footer contains a UTC ISO timestamp (+00:00)."""
        anomaly = _make_anomaly(
            AnomalyType.FAILURE_LOOP, issue_number=None, pr_number=None
        )
        footer = service._build_footer(anomaly)
        assert "+00:00" in footer

    def test_footer_has_separator(self, service: InterventionService) -> None:
        """Footer starts with markdown horizontal rule separator."""
        anomaly = _make_anomaly(issue_number=None)
        footer = service._build_footer(anomaly)
        assert "---" in footer


# ── _do_create_issue() ────────────────────────────────────────────────────────


class TestCreateIssueFooter:
    """Tests for _do_create_issue() appending footer."""

    def test_footer_appended_to_body(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        """_do_create_issue() appends footer to body before create_issue()."""
        plan = InterventionPlan(
            action=InterventionAction.CREATE_ISSUE,
            reason="bug",
            details="",
            anomaly=_make_anomaly(
                AnomalyType.FAILURE_LOOP, issue_number=10, pr_number=None
            ),
            new_issue_title="Fix bug",
            new_issue_body="## Overview\nSome spec",
        )

        assert service.execute(plan) is True
        body_passed = github.create_issue.call_args[0][1]
        assert "## Overview\nSome spec" in body_passed
        assert "---" in body_passed
        assert "issue #10" in body_passed

    def test_labels_unchanged(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        """Labels status:ready and orchestrator are passed unchanged."""
        plan = InterventionPlan(
            action=InterventionAction.CREATE_ISSUE,
            reason="r",
            details="",
            anomaly=_make_anomaly(issue_number=None, pr_number=None),
            new_issue_title="title",
            new_issue_body="body",
        )

        service.execute(plan)
        kwargs = github.create_issue.call_args[1]
        assert "status:ready" in kwargs.get("labels", [])
        assert "orchestrator" in kwargs.get("labels", [])

    def test_no_source_line_when_no_issue_or_pr(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        """Footer has no Source line when anomaly has no issue_number or pr_number."""
        plan = InterventionPlan(
            action=InterventionAction.CREATE_ISSUE,
            reason="r",
            details="",
            anomaly=_make_anomaly(
                AnomalyType.AGENT_CRASH, issue_number=None, pr_number=None
            ),
            new_issue_title="title",
            new_issue_body="body",
        )

        service.execute(plan)
        body_passed = github.create_issue.call_args[0][1]
        assert "Source:" not in body_passed

    def test_cross_link_comment_posted_on_source_issue(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        """When source issue exists, a cross-link comment is posted there."""
        plan = InterventionPlan(
            action=InterventionAction.CREATE_ISSUE,
            reason="r",
            details="",
            anomaly=_make_anomaly(
                AnomalyType.FAILURE_LOOP, issue_number=42, pr_number=None
            ),
            new_issue_title="title",
            new_issue_body="body",
        )

        service.execute(plan)
        github.comment_issue.assert_called_once()
        call_args = github.comment_issue.call_args[0]
        assert call_args[0] == 42


# ── _generate_issue_spec() ────────────────────────────────────────────────────


class TestGenerateIssueSpec:
    """Tests for _generate_issue_spec() prompt includes anomaly context."""

    def test_prompt_includes_anomaly_context(
        self, service: InterventionService
    ) -> None:
        """Prompt sent to Claude includes anomaly type, detail, and source."""
        anomaly = _make_anomaly(
            AnomalyType.FAILURE_LOOP, issue_number=20, detail="loop detected"
        )
        captured_prompts: list[str] = []

        def capture(prompt: str) -> str:
            """Capture the prompt and return valid JSON."""
            captured_prompts.append(prompt)
            return json.dumps({"title": "Fix loop", "body": "## Overview\nFix it"})

        with patch.object(service, "_call_claude", side_effect=capture):
            service._generate_issue_spec(anomaly)

        assert captured_prompts, "Claude was never called"
        prompt = captured_prompts[0]
        assert "failure_loop" in prompt
        assert "loop detected" in prompt
        assert "issue #20" in prompt

    def test_prompt_uses_system_anomaly_when_no_source(
        self, service: InterventionService
    ) -> None:
        """When no source reference, prompt includes 'N/A (system anomaly)'."""
        anomaly = _make_anomaly(
            AnomalyType.AGENT_CRASH, issue_number=None, pr_number=None
        )
        captured: list[str] = []

        with patch.object(
            service,
            "_call_claude",
            side_effect=lambda p: captured.append(p)
            or json.dumps({"title": "t", "body": "b"}),
        ):
            service._generate_issue_spec(anomaly)

        assert "N/A (system anomaly)" in captured[0]


# ── uv PATH fix ───────────────────────────────────────────────────────────────


class TestUvPathFix:
    """Tests for uv executable resolution in worker_common._find_uv()."""

    def test_find_uv_returns_string(self) -> None:
        """_find_uv() always returns a non-empty string."""
        from workflow_engine.worker_common import _find_uv

        result = _find_uv()
        assert isinstance(result, str)
        assert result  # not empty

    def test_find_uv_prefers_shutil_which(self) -> None:
        """_find_uv() returns shutil.which result when uv is on PATH."""
        from workflow_engine.worker_common import _find_uv

        with patch(
            "workflow_engine.worker_common.shutil.which", return_value="/usr/bin/uv"
        ):
            result = _find_uv()
        assert result == "/usr/bin/uv"

    def test_find_uv_falls_back_to_local_bin(self, tmp_path: Path) -> None:
        """_find_uv() falls back to ~/.local/bin/uv when not on PATH."""
        from workflow_engine.worker_common import _find_uv

        fake_uv = tmp_path / "uv"
        fake_uv.touch()

        with (
            patch("workflow_engine.worker_common.shutil.which", return_value=None),
            patch("workflow_engine.worker_common.Path.home", return_value=tmp_path),
        ):
            result = _find_uv()

        assert result == str(tmp_path / ".local" / "bin" / "uv") or result == "uv"

    def test_find_uv_does_not_raise(self) -> None:
        """_find_uv() never raises FileNotFoundError — returns string fallback."""
        from workflow_engine.worker_common import _find_uv

        with patch("workflow_engine.worker_common.shutil.which", return_value=None):
            result = _find_uv()
        assert isinstance(result, str)
