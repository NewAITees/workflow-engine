"""Tests for Planner escalation loop logic."""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from shared.github_client import Issue

spec = importlib.util.spec_from_file_location(
    "planner_main", Path(__file__).parent.parent / "planner-agent" / "main.py"
)
if spec is None or spec.loader is None:
    raise ImportError("Failed to load planner-agent/main.py")

planner_main = importlib.util.module_from_spec(spec)
sys.modules["planner_main"] = planner_main
spec.loader.exec_module(planner_main)
PlannerAgent = planner_main.PlannerAgent


class TestPlannerAgent:
    """Tests for planner escalation processing."""

    @patch("planner_main.LLMClient")
    @patch("planner_main.GitHubClient")
    @patch("planner_main.get_agent_config")
    def test_process_escalated_issue_success(
        self, mock_config, mock_github, mock_llm
    ) -> None:
        mock_config.return_value = MagicMock(
            gh_cli="gh", llm_backend="codex", poll_interval=30
        )
        llm = mock_llm.return_value
        llm.create_spec.return_value = MagicMock(success=True, output="Revised spec")

        agent = PlannerAgent("owner/repo")
        agent.github.get_issue = MagicMock(
            return_value=Issue(
                number=1,
                title="Issue",
                body="Current spec",
                labels=[agent.STATUS_ESCALATED],
            )
        )
        agent.github.get_issue_comments = MagicMock(
            return_value=[
                {
                    "body": "ESCALATION:worker\nNeed clearer acceptance criteria.",
                    "created_at": "2026-02-09T07:00:00Z",
                }
            ]
        )
        agent.github.update_issue_body = MagicMock(return_value=True)
        agent.github.comment_issue = MagicMock(return_value=True)
        agent.github.remove_label = MagicMock(return_value=True)
        agent.github.add_label = MagicMock(return_value=True)

        result = agent._try_process_escalated_issue(1)

        assert result is True
        agent.github.update_issue_body.assert_called_once_with(1, "Revised spec")
        assert any(
            "PLANNER_RETRY:1" in str(call)
            for call in agent.github.comment_issue.call_args_list
        )
        agent.github.add_label.assert_called_with(1, agent.STATUS_READY)

    @patch("planner_main.LLMClient")
    @patch("planner_main.GitHubClient")
    @patch("planner_main.get_agent_config")
    def test_process_escalated_issue_skips_if_no_new_escalation(
        self, mock_config, mock_github, mock_llm
    ) -> None:
        mock_config.return_value = MagicMock(
            gh_cli="gh", llm_backend="codex", poll_interval=30
        )
        mock_llm.return_value.create_spec.return_value = MagicMock(
            success=True, output="ignored"
        )

        agent = PlannerAgent("owner/repo")
        agent.github.get_issue = MagicMock(
            return_value=Issue(
                number=2,
                title="Issue",
                body="Current spec",
                labels=[agent.STATUS_ESCALATED],
            )
        )
        agent.github.get_issue_comments = MagicMock(
            return_value=[
                {
                    "body": "ESCALATION:reviewer\nNeed redesign.",
                    "created_at": "2026-02-09T07:00:00Z",
                },
                {
                    "body": "Planner done.\nPLANNER_RETRY:1",
                    "created_at": "2026-02-09T07:10:00Z",
                },
            ]
        )
        agent.github.update_issue_body = MagicMock(return_value=True)

        result = agent._try_process_escalated_issue(2)

        assert result is False
        agent.github.update_issue_body.assert_not_called()

    @patch("planner_main.LLMClient")
    @patch("planner_main.GitHubClient")
    @patch("planner_main.get_agent_config")
    def test_process_escalated_issue_marks_failed_at_retry_limit(
        self, mock_config, mock_github, mock_llm
    ) -> None:
        mock_config.return_value = MagicMock(
            gh_cli="gh", llm_backend="codex", poll_interval=30
        )
        mock_llm.return_value.create_spec.return_value = MagicMock(
            success=True, output="ignored"
        )

        agent = PlannerAgent("owner/repo")
        agent.github.get_issue = MagicMock(
            return_value=Issue(
                number=3,
                title="Issue",
                body="Current spec",
                labels=[agent.STATUS_ESCALATED],
            )
        )
        agent.github.get_issue_comments = MagicMock(
            return_value=[
                {
                    "body": "ESCALATION:worker\nStill failing.",
                    "created_at": "2026-02-09T08:00:00Z",
                },
                {
                    "body": "PLANNER_RETRY:3",
                    "created_at": "2026-02-09T07:50:00Z",
                },
            ]
        )
        agent.github.comment_issue = MagicMock(return_value=True)
        agent.github.remove_label = MagicMock(return_value=True)
        agent.github.add_label = MagicMock(return_value=True)
        agent.github.update_issue_body = MagicMock(return_value=True)

        result = agent._try_process_escalated_issue(3)

        assert result is True
        agent.github.update_issue_body.assert_not_called()
        agent.github.add_label.assert_any_call(3, agent.STATUS_FAILED)

    @patch("planner_main.LLMClient")
    @patch("planner_main.GitHubClient")
    @patch("planner_main.get_agent_config")
    def test_failed_issue_is_auto_bridged_to_escalation(
        self, mock_config, mock_github, mock_llm
    ) -> None:
        mock_config.return_value = MagicMock(
            gh_cli="gh", llm_backend="codex", poll_interval=30
        )
        mock_llm.return_value.create_spec.return_value = MagicMock(
            success=True, output="Revised spec from failed"
        )

        agent = PlannerAgent("owner/repo")
        agent.github.get_issue = MagicMock(
            return_value=Issue(
                number=4,
                title="Failed issue",
                body="Current spec",
                labels=[agent.STATUS_FAILED],
            )
        )
        agent.github.get_issue_comments = MagicMock(
            side_effect=[
                [
                    {
                        "body": "❌ **Processing failed**\n\n```\npush failed\n```",
                        "created_at": "2026-02-09T07:00:00Z",
                    }
                ],
                [
                    {
                        "body": "❌ **Processing failed**\n\n```\npush failed\n```",
                        "created_at": "2026-02-09T07:00:00Z",
                    },
                    {
                        "body": "ESCALATION:worker\n\nReason: auto-bridge-from-status-failed",
                        "created_at": "2026-02-09T07:01:00Z",
                    },
                ],
            ]
        )
        agent.github.comment_issue = MagicMock(return_value=True)
        agent.github.update_issue_body = MagicMock(return_value=True)
        agent.github.remove_label = MagicMock(return_value=True)
        agent.github.add_label = MagicMock(return_value=True)

        result = agent._try_process_escalated_issue(4)

        assert result is True
        assert any(
            "auto-bridge-from-status-failed" in str(call)
            for call in agent.github.comment_issue.call_args_list
        )
        agent.github.update_issue_body.assert_called_once_with(
            4, "Revised spec from failed"
        )

    @patch("planner_main.LLMClient")
    @patch("planner_main.GitHubClient")
    @patch("planner_main.get_agent_config")
    def test_stale_escalation_triggers_new_bridge(
        self, mock_config, mock_github, mock_llm
    ) -> None:
        """When status:failed with stale escalation, Planner creates a new bridge."""
        mock_config.return_value = MagicMock(
            gh_cli="gh", llm_backend="codex", poll_interval=30
        )
        mock_llm.return_value.create_spec.return_value = MagicMock(
            success=True, output="Revised spec v2"
        )

        agent = PlannerAgent("owner/repo")
        agent.github.get_issue = MagicMock(
            return_value=Issue(
                number=5,
                title="Re-failed issue",
                body="Current spec",
                labels=[agent.STATUS_FAILED],
            )
        )
        agent.github.get_issue_comments = MagicMock(
            side_effect=[
                # First read: stale escalation + newer retry
                [
                    {
                        "body": "ESCALATION:worker\nOriginal failure.",
                        "created_at": "2026-02-09T07:00:00Z",
                    },
                    {
                        "body": "PLANNER_RETRY:1",
                        "created_at": "2026-02-09T07:10:00Z",
                    },
                ],
                # Second read: after new bridge comment is posted
                [
                    {
                        "body": "ESCALATION:worker\nOriginal failure.",
                        "created_at": "2026-02-09T07:00:00Z",
                    },
                    {
                        "body": "PLANNER_RETRY:1",
                        "created_at": "2026-02-09T07:10:00Z",
                    },
                    {
                        "body": "ESCALATION:worker\n\nReason: auto-bridge-from-status-failed",
                        "created_at": "2026-02-09T08:00:00Z",
                    },
                ],
            ]
        )
        agent.github.comment_issue = MagicMock(return_value=True)
        agent.github.update_issue_body = MagicMock(return_value=True)
        agent.github.remove_label = MagicMock(return_value=True)
        agent.github.add_label = MagicMock(return_value=True)

        result = agent._try_process_escalated_issue(5)

        assert result is True
        agent.github.update_issue_body.assert_called_once_with(5, "Revised spec v2")

    @patch("planner_main.LLMClient")
    @patch("planner_main.GitHubClient")
    @patch("planner_main.get_agent_config")
    def test_stale_escalation_exhausted_retries_skips(
        self, mock_config, mock_github, mock_llm
    ) -> None:
        """When stale escalation + max retries reached, Planner skips silently."""
        mock_config.return_value = MagicMock(
            gh_cli="gh", llm_backend="codex", poll_interval=30
        )

        agent = PlannerAgent("owner/repo")
        agent.github.get_issue = MagicMock(
            return_value=Issue(
                number=6,
                title="Exhausted issue",
                body="Current spec",
                labels=[agent.STATUS_FAILED],
            )
        )
        agent.github.get_issue_comments = MagicMock(
            return_value=[
                {
                    "body": "ESCALATION:worker\nFailure.",
                    "created_at": "2026-02-09T07:00:00Z",
                },
                {
                    "body": "PLANNER_RETRY:3",
                    "created_at": "2026-02-09T07:10:00Z",
                },
            ]
        )
        agent.github.comment_issue = MagicMock(return_value=True)
        agent.github.update_issue_body = MagicMock(return_value=True)

        result = agent._try_process_escalated_issue(6)

        assert result is True
        agent.github.update_issue_body.assert_not_called()
        # Should NOT create a new escalation bridge
        assert not any(
            "auto-bridge" in str(call)
            for call in agent.github.comment_issue.call_args_list
        )

    @patch("planner_main.LLMClient")
    @patch("planner_main.GitHubClient")
    @patch("planner_main.get_agent_config")
    def test_process_escalations_scans_only_escalated_and_failed(
        self, mock_config, mock_github, mock_llm
    ) -> None:
        mock_config.return_value = MagicMock(
            gh_cli="gh", llm_backend="codex", poll_interval=30
        )
        mock_llm.return_value.create_spec.return_value = MagicMock(
            success=True, output="ignored"
        )

        agent = PlannerAgent("owner/repo")
        escalated_issue = Issue(
            number=10,
            title="Escalated",
            body="spec",
            labels=[agent.STATUS_ESCALATED],
        )
        failed_issue = Issue(
            number=20,
            title="Failed",
            body="spec",
            labels=[agent.STATUS_FAILED],
        )
        agent.github.list_issues = MagicMock(
            side_effect=[[escalated_issue], [failed_issue]]
        )
        agent._try_process_escalated_issue = MagicMock(side_effect=[False, False])

        result = agent._process_escalations(limit=20)

        assert result is False
        assert agent.github.list_issues.call_count == 2
        agent.github.list_issues.assert_any_call(
            labels=[agent.STATUS_ESCALATED], state="open", limit=100
        )
        agent.github.list_issues.assert_any_call(
            labels=[agent.STATUS_FAILED], state="open", limit=100
        )
        agent._try_process_escalated_issue.assert_any_call(10, issue=escalated_issue)
        agent._try_process_escalated_issue.assert_any_call(20, issue=failed_issue)
