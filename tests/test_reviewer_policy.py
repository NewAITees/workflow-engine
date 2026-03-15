"""Tests for policy candidate saving in reviewer-agent/main.py."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from reviewer_agent_main import ReviewerAgent, _format_policy_candidate_comment

from shared.policy_store import STATUS_DRAFT, PolicyStore

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_agent(tmp_path) -> ReviewerAgent:
    """Return a ReviewerAgent with mocked external dependencies."""
    with patch("reviewer_agent_main.get_agent_config") as mock_cfg:
        cfg = MagicMock()
        cfg.policy_db = str(tmp_path / "policies.db")
        cfg.llm_backend = "codex"
        cfg.gh_cli = "gh"
        cfg.auto_merge = False
        mock_cfg.return_value = cfg

        with (
            patch("reviewer_agent_main.GitHubClient"),
            patch("reviewer_agent_main.LockManager"),
            patch("reviewer_agent_main.LLMClient"),
        ):
            agent = ReviewerAgent("owner/repo")
            agent.config = cfg
            return agent


def _make_issue(number: int = 10) -> MagicMock:
    issue = MagicMock()
    issue.number = number
    return issue


def _candidate(title: str = "Check tests first") -> dict:
    return {
        "title": title,
        "why": "Past bugs introduced by untested code",
        "rules": ["Run tests before merging", "Add test for every fix"],
        "strength": "medium",
        "trigger_tags": ["bugfix"],
        "trigger_conditions": [],
    }


# ── _format_policy_candidate_comment ─────────────────────────────────────────


class TestFormatPolicyCandidateComment:
    def test_contains_header(self) -> None:
        comment = _format_policy_candidate_comment([_candidate()], ["policy_abc123"])
        assert "## 🔍 Policy Candidates from Review" in comment

    def test_contains_policy_id(self) -> None:
        comment = _format_policy_candidate_comment([_candidate()], ["policy_abc123"])
        assert "policy_abc123" in comment

    def test_contains_title(self) -> None:
        comment = _format_policy_candidate_comment(
            [_candidate("My Rule")], ["policy_abc123"]
        )
        assert "My Rule" in comment

    def test_contains_rules(self) -> None:
        comment = _format_policy_candidate_comment([_candidate()], ["policy_abc123"])
        assert "Run tests before merging" in comment

    def test_multiple_candidates(self) -> None:
        cands = [_candidate("Rule A"), _candidate("Rule B")]
        ids = ["policy_aaa", "policy_bbb"]
        comment = _format_policy_candidate_comment(cands, ids)
        assert "policy_aaa" in comment
        assert "policy_bbb" in comment
        assert "Rule A" in comment
        assert "Rule B" in comment


# ── ReviewerAgent._save_policy_candidates ────────────────────────────────────


class TestSavePolicyCandidates:
    def test_skips_when_no_candidates(self, tmp_path) -> None:
        agent = _make_agent(tmp_path)
        # Should complete without error and without touching PolicyStore
        agent._save_policy_candidates([], linked_issue=None)

    def test_skips_when_policy_db_not_configured(self, tmp_path) -> None:
        agent = _make_agent(tmp_path)
        agent.config.policy_db = None

        # Verify no DB is created
        agent._save_policy_candidates([_candidate()], linked_issue=None)
        assert not (tmp_path / "policies.db").exists()

    def test_saves_candidate_as_draft(self, tmp_path) -> None:
        agent = _make_agent(tmp_path)
        agent._save_policy_candidates([_candidate()], linked_issue=None)

        store = PolicyStore(agent.config.policy_db)
        try:
            drafts = store.query(status=STATUS_DRAFT)
            assert len(drafts) == 1
            assert drafts[0].title == "Check tests first"
        finally:
            store.close()

    def test_saves_source_task_from_linked_issue(self, tmp_path) -> None:
        agent = _make_agent(tmp_path)
        issue = _make_issue(number=42)

        agent._save_policy_candidates([_candidate()], linked_issue=issue)

        store = PolicyStore(agent.config.policy_db)
        try:
            drafts = store.query(status=STATUS_DRAFT)
            assert drafts[0].source_task == "42"
        finally:
            store.close()

    def test_saves_multiple_candidates(self, tmp_path) -> None:
        agent = _make_agent(tmp_path)
        cands = [_candidate("Rule A"), _candidate("Rule B")]

        agent._save_policy_candidates(cands, linked_issue=None)

        store = PolicyStore(agent.config.policy_db)
        try:
            drafts = store.query(status=STATUS_DRAFT)
            assert len(drafts) == 2
        finally:
            store.close()

    def test_posts_comment_to_linked_issue(self, tmp_path) -> None:
        agent = _make_agent(tmp_path)
        issue = _make_issue(number=99)

        agent._save_policy_candidates([_candidate()], linked_issue=issue)

        agent.github.comment_issue.assert_called_once()
        call_args = agent.github.comment_issue.call_args
        assert call_args[0][0] == 99
        assert "Policy Candidates" in call_args[0][1]

    def test_no_comment_when_no_linked_issue(self, tmp_path) -> None:
        agent = _make_agent(tmp_path)
        agent._save_policy_candidates([_candidate()], linked_issue=None)
        agent.github.comment_issue.assert_not_called()

    def test_store_closed_after_save(self, tmp_path) -> None:
        """PolicyStore must be closed (released) after saving."""
        agent = _make_agent(tmp_path)
        closed_stores: list[PolicyStore] = []
        original_close = PolicyStore.close

        def tracking_close(self_store: PolicyStore) -> None:
            closed_stores.append(self_store)
            original_close(self_store)

        with patch.object(PolicyStore, "close", tracking_close):
            agent._save_policy_candidates([_candidate()], linked_issue=None)

        assert len(closed_stores) == 1
