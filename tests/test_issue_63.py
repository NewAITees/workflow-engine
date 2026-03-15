"""Tests for InterventionService._build_context() context enrichment (Issue #63).

Validates that _build_context() fetches the latest failure comment from GitHub
for FAILURE_LOOP and CI_LOOP anomalies and appends it to the context string.
"""

from unittest.mock import MagicMock

import pytest

from orchestrator.intervention import InterventionService
from orchestrator.monitor import Anomaly, AnomalyType

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_anomaly(
    anomaly_type: AnomalyType,
    issue_number: int | None = 63,
    detail: str = "test detail",
    pr_number: int | None = None,
) -> Anomaly:
    """Create a minimal Anomaly for testing.

    Args:
        anomaly_type: The type of anomaly.
        issue_number: GitHub issue number (None for PR-only anomalies).
        detail: Human-readable detail string.
        pr_number: GitHub PR number, if applicable.

    Returns:
        A configured Anomaly instance.
    """
    return Anomaly(
        anomaly_type=anomaly_type,
        detail=detail,
        issue_number=issue_number,
        pr_number=pr_number,
    )


def _make_comment(body: str) -> dict:
    """Create a minimal comment dict matching GitHubClient.get_issue_comments() output.

    Args:
        body: Comment body text.

    Returns:
        A dict with a ``body`` key.
    """
    return {"body": body}


@pytest.fixture
def github() -> MagicMock:
    """Return a MagicMock configured with common GitHubClient return values."""
    mock = MagicMock()
    mock.get_issue_comments.return_value = []
    return mock


@pytest.fixture
def service(github: MagicMock) -> InterventionService:
    """Return an InterventionService backed by the mock GitHub client."""
    return InterventionService(github)


# ── FAILURE_LOOP context enrichment ───────────────────────────────────────────


class TestBuildContextFailureLoop:
    """_build_context() includes latest failure comment for FAILURE_LOOP anomalies."""

    def test_branch_duplicate_error_included_in_context(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        """Failure comment with branch-already-exists error is added to context.

        Validates acceptance criterion: FAILURE_LOOP anomaly → ❌ **Processing failed**
        comment is fetched and included.
        """
        error_body = (
            "❌ **Processing failed**\n\n"
            "Failed to create branch: fatal: a branch named 'auto/issue-63' already exists"
        )
        github.get_issue_comments.return_value = [_make_comment(error_body)]

        anomaly = _make_anomaly(AnomalyType.FAILURE_LOOP, issue_number=63)
        context = service._build_context(anomaly)

        assert "latest_failure_comment" in context
        assert "fatal: a branch named 'auto/issue-63' already exists" in context

    def test_node_not_found_error_included_in_context(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        """Failure comment with node-not-found error is added to context.

        Validates acceptance criterion: concrete tool-install error is surfaced
        so Claude can decide to escalate to human review.
        """
        error_body = (
            "❌ **Processing failed**\n\n"
            "Test generation failed: /mnt/c/Users/perso/AppData/Roaming/npm/codex: 15: "
            "exec: node: not found"
        )
        github.get_issue_comments.return_value = [_make_comment(error_body)]

        anomaly = _make_anomaly(AnomalyType.FAILURE_LOOP, issue_number=10)
        context = service._build_context(anomaly)

        assert "latest_failure_comment" in context
        assert "exec: node: not found" in context

    def test_only_latest_matching_comment_is_used(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        """When multiple matching comments exist, only the most recent one is used.

        The comment list is returned oldest-first; the last matching entry wins.
        """
        older = _make_comment("❌ **Processing failed**\n\nold error")
        newer = _make_comment("❌ **Processing failed**\n\nnew error")
        github.get_issue_comments.return_value = [older, newer]

        anomaly = _make_anomaly(AnomalyType.FAILURE_LOOP, issue_number=63)
        context = service._build_context(anomaly)

        assert "new error" in context
        assert "old error" not in context

    def test_non_matching_comments_are_ignored(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        """Comments that don't start with the expected prefix are not included.

        Validates edge case: no match → latest_failure_comment field absent.
        """
        github.get_issue_comments.return_value = [
            _make_comment("ACK:worker:worker-abc:123"),
            _make_comment("Some other comment"),
        ]

        anomaly = _make_anomaly(AnomalyType.FAILURE_LOOP, issue_number=63)
        context = service._build_context(anomaly)

        assert "latest_failure_comment" not in context

    def test_empty_comment_list_does_not_add_field(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        """Empty comment list means latest_failure_comment is omitted.

        Validates edge case: get_issue_comments returns [] → field absent.
        """
        github.get_issue_comments.return_value = []

        anomaly = _make_anomaly(AnomalyType.FAILURE_LOOP, issue_number=63)
        context = service._build_context(anomaly)

        assert "latest_failure_comment" not in context


# ── CI_LOOP context enrichment ─────────────────────────────────────────────────


class TestBuildContextCiLoop:
    """_build_context() includes latest CI failure comment for CI_LOOP anomalies."""

    def test_ci_failed_comment_included(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        """⚠️ **CI failed** comment is added to context for CI_LOOP anomaly.

        Validates acceptance criterion for CI_LOOP enrichment.
        """
        ci_body = "⚠️ **CI failed**\n\nruff: E501 line too long (120 > 88)"
        github.get_issue_comments.return_value = [_make_comment(ci_body)]

        anomaly = _make_anomaly(AnomalyType.CI_LOOP, issue_number=99)
        context = service._build_context(anomaly)

        assert "latest_failure_comment" in context
        assert "ruff: E501 line too long" in context

    def test_processing_failed_prefix_not_used_for_ci_loop(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        """FAILURE_LOOP prefix (❌) is not matched for CI_LOOP anomalies.

        Each anomaly type uses its own prefix to avoid cross-contamination.
        """
        processing_fail = _make_comment("❌ **Processing failed**\n\nbranch error")
        ci_fail = _make_comment("⚠️ **CI failed**\n\nci error detail")
        github.get_issue_comments.return_value = [processing_fail, ci_fail]

        anomaly = _make_anomaly(AnomalyType.CI_LOOP, issue_number=99)
        context = service._build_context(anomaly)

        assert "ci error detail" in context
        assert "branch error" not in context


# ── issue_number=None guard ────────────────────────────────────────────────────


class TestBuildContextNoIssueNumber:
    """_build_context() skips API call when issue_number is None."""

    def test_no_api_call_when_issue_number_is_none(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        """get_issue_comments is never called for PR-only (issue_number=None) anomalies.

        Validates acceptance criterion: anomaly.issue_number is None → 0 API calls.
        """
        anomaly = _make_anomaly(
            AnomalyType.FAILURE_LOOP, issue_number=None, pr_number=5
        )
        service._build_context(anomaly)

        github.get_issue_comments.assert_not_called()

    def test_context_built_without_failure_comment_when_no_issue(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        """Context is still built with base fields when issue_number is None.

        Ensures the method doesn't crash and returns a valid context string.
        """
        anomaly = _make_anomaly(AnomalyType.CI_LOOP, issue_number=None, pr_number=7)
        context = service._build_context(anomaly)

        assert "latest_failure_comment" not in context
        assert len(context) > 0


# ── Non-enriched anomaly types ─────────────────────────────────────────────────


class TestBuildContextOtherAnomalyTypes:
    """Other anomaly types (e.g. STALE_IMPLEMENTING, SPEC_BLOAT) are not enriched."""

    @pytest.mark.parametrize(
        "anomaly_type",
        [
            AnomalyType.STALE_IMPLEMENTING,
            AnomalyType.SPEC_BLOAT,
            AnomalyType.AGENT_CRASH,
        ],
    )
    def test_no_api_call_for_non_failure_types(
        self,
        anomaly_type: AnomalyType,
        service: InterventionService,
        github: MagicMock,
    ) -> None:
        """get_issue_comments is not called for anomaly types outside FAILURE_LOOP/CI_LOOP.

        Validates edge case: FAILURE_COMMENT_PREFIXES lookup returns None → skip.

        Args:
            anomaly_type: The anomaly type to exercise.
            service: The InterventionService under test.
            github: The mock GitHub client.
        """
        anomaly = _make_anomaly(anomaly_type, issue_number=63)
        service._build_context(anomaly)

        github.get_issue_comments.assert_not_called()


# ── Truncation ─────────────────────────────────────────────────────────────────


class TestBuildContextTruncation:
    """Context and comment body truncation limits are enforced."""

    def test_context_truncated_to_3000_chars(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        """Total context is cut at 3000 characters.

        Validates acceptance criterion: context[:3000] applied to final string.
        """
        long_comment = "❌ **Processing failed**\n\n" + "x" * 4000
        github.get_issue_comments.return_value = [_make_comment(long_comment)]

        anomaly = _make_anomaly(
            AnomalyType.FAILURE_LOOP,
            issue_number=63,
            detail="issue-63 has failed 3 times",
        )
        context = service._build_context(anomaly)

        assert len(context) <= 3000

    def test_comment_body_truncated_to_1500_chars(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        """Comment body is sliced to 1500 characters before insertion.

        Validates that body[:1500] is applied, keeping the 3000-char overall cap stable.
        """
        prefix = "❌ **Processing failed**\n\n"
        padding = "y" * 2000
        github.get_issue_comments.return_value = [_make_comment(prefix + padding)]

        anomaly = _make_anomaly(AnomalyType.FAILURE_LOOP, issue_number=63)
        context = service._build_context(anomaly)

        # The snippet in context must be at most 1500 chars of the comment body
        assert len(context) <= 3000
        # The full 2000-char padding should not appear intact in the context
        assert padding not in context

    def test_code_blocks_preserved_in_context(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        """Code blocks inside failure comments are included as-is (no reformatting).

        Validates requirement: markdown code blocks need not be stripped or escaped.
        """
        body_with_code = (
            "❌ **Processing failed**\n\n"
            "```\nTraceback (most recent call last):\n  File ...\nValueError: bad\n```"
        )
        github.get_issue_comments.return_value = [_make_comment(body_with_code)]

        anomaly = _make_anomaly(AnomalyType.FAILURE_LOOP, issue_number=63)
        context = service._build_context(anomaly)

        assert "```" in context
        assert "ValueError: bad" in context


# ── Exception resilience ───────────────────────────────────────────────────────


class TestBuildContextExceptionHandling:
    """get_issue_comments() exceptions are swallowed; decide()/execute() still work."""

    def test_get_issue_comments_exception_does_not_raise(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        """_build_context() catches exceptions from get_issue_comments and continues.

        Validates acceptance criterion: exception → logger.warning, no re-raise.
        """
        github.get_issue_comments.side_effect = RuntimeError("network error")

        anomaly = _make_anomaly(AnomalyType.FAILURE_LOOP, issue_number=63)
        # Must not raise
        context = service._build_context(anomaly)

        assert isinstance(context, str)
        assert "latest_failure_comment" not in context

    def test_get_issue_comments_exception_logs_warning(
        self,
        service: InterventionService,
        github: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A warning is logged when get_issue_comments raises.

        Uses caplog to assert logger.warning was called with the error.

        Args:
            service: The InterventionService under test.
            github: The mock GitHub client.
            caplog: pytest log capture fixture.
        """
        import logging

        github.get_issue_comments.side_effect = ValueError("api error")
        anomaly = _make_anomaly(AnomalyType.FAILURE_LOOP, issue_number=63)

        with caplog.at_level(logging.WARNING, logger="orchestrator.intervention"):
            service._build_context(anomaly)

        assert any(
            "api error" in r.message or "Failed to fetch" in r.message
            for r in caplog.records
        )

    def test_decide_succeeds_when_get_issue_comments_raises(
        self, service: InterventionService, github: MagicMock
    ) -> None:
        """decide() returns a valid InterventionPlan even when comment fetch fails.

        Validates acceptance criterion: decide() / execute() interface unchanged.
        """
        import json
        from unittest.mock import patch

        from orchestrator.intervention import InterventionAction

        github.get_issue_comments.side_effect = ConnectionError("timeout")

        anomaly = _make_anomaly(AnomalyType.FAILURE_LOOP, issue_number=63)
        with patch.object(
            service,
            "_call_claude",
            return_value=json.dumps(
                {"action": "mark_manual", "reason": "fallback", "details": ""}
            ),
        ):
            plan = service.decide(anomaly)

        assert plan.action == InterventionAction.MARK_MANUAL
