"""Tests for PlannerAgent._check_policy_approvals()."""

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.github_client import Issue
from shared.policy_store import STATUS_ACTIVE, STATUS_DRAFT, PolicyStore

# Load planner-agent/main.py as planner_agent_main
_repo_root = Path(__file__).parent.parent
_planner_dir = _repo_root / "planner-agent"
_spec = importlib.util.spec_from_file_location(
    "planner_agent_main",
    _planner_dir / "main.py",
    submodule_search_locations=[],
)
if _spec and _spec.loader and "planner_agent_main" not in sys.modules:
    _mod = types.ModuleType("planner_agent_main")
    _mod.__file__ = str(_planner_dir / "main.py")
    sys.modules["planner_agent_main"] = _mod
    _spec.loader.exec_module(_mod)  # type: ignore[union-attr]

from planner_agent_main import PlannerAgent  # noqa: E402

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_agent(tmp_path) -> PlannerAgent:
    with patch("planner_agent_main.get_agent_config") as mock_cfg:
        cfg = MagicMock()
        cfg.policy_db = str(tmp_path / "policies.db")
        cfg.llm_backend = "codex"
        cfg.gh_cli = "gh"
        mock_cfg.return_value = cfg

        with (
            patch("planner_agent_main.GitHubClient"),
            patch("planner_agent_main.LLMClient"),
        ):
            agent = PlannerAgent("owner/repo")
            agent.config = cfg
            return agent


def _make_issue(number: int, labels: list[str] | None = None) -> Issue:
    return Issue(
        number=number,
        title="Test issue",
        body="Issue body",
        labels=labels or [],
        state="open",
    )


def _insert_draft(
    store: PolicyStore,
    title: str = "Read tests first",
    source_task: str | None = "10",
) -> str:
    pid = store.insert_candidate(
        {
            "title": title,
            "why": "Past bugs",
            "rules": ["Write tests before implementation"],
            "strength": "medium",
            "trigger_tags": ["bugfix"],
            "trigger_conditions": [],
        },
        source_task=source_task,
    )
    return pid


# ── _check_policy_approvals ───────────────────────────────────────────────────


class TestCheckPolicyApprovals:
    def test_skips_when_policy_db_not_configured(self, tmp_path) -> None:
        agent = _make_agent(tmp_path)
        agent.config.policy_db = None
        # Should not raise or call GitHub
        agent._check_policy_approvals()
        agent.github.get_issue.assert_not_called()

    def test_no_action_when_no_draft_policies(self, tmp_path) -> None:
        agent = _make_agent(tmp_path)
        # Empty store
        PolicyStore(agent.config.policy_db).close()
        agent._check_policy_approvals()
        agent.github.get_issue.assert_not_called()

    def test_no_action_when_label_absent(self, tmp_path) -> None:
        agent = _make_agent(tmp_path)
        store = PolicyStore(agent.config.policy_db)
        _insert_draft(store, source_task="10")
        store.close()

        agent.github.get_issue.return_value = _make_issue(10, labels=[])
        agent._check_policy_approvals()

        agent.github.comment_issue.assert_not_called()
        agent.github.remove_label.assert_not_called()

    def test_approves_policy_when_label_present(self, tmp_path) -> None:
        agent = _make_agent(tmp_path)
        store = PolicyStore(agent.config.policy_db)
        pid = _insert_draft(store, title="My Policy", source_task="10")
        store.close()

        agent.github.get_issue.return_value = _make_issue(10, labels=["approve-policy"])
        agent._check_policy_approvals()

        # Verify policy is now active
        store2 = PolicyStore(agent.config.policy_db)
        policy = store2.get(pid)
        store2.close()
        assert policy is not None
        assert policy.status == STATUS_ACTIVE

    def test_posts_confirmation_comment(self, tmp_path) -> None:
        agent = _make_agent(tmp_path)
        store = PolicyStore(agent.config.policy_db)
        pid = _insert_draft(store, title="My Policy", source_task="10")
        store.close()

        agent.github.get_issue.return_value = _make_issue(10, labels=["approve-policy"])
        agent._check_policy_approvals()

        agent.github.comment_issue.assert_called_once()
        args = agent.github.comment_issue.call_args[0]
        assert args[0] == 10
        assert pid in args[1]
        assert "My Policy" in args[1]

    def test_removes_approve_policy_label(self, tmp_path) -> None:
        agent = _make_agent(tmp_path)
        store = PolicyStore(agent.config.policy_db)
        _insert_draft(store, source_task="10")
        store.close()

        agent.github.get_issue.return_value = _make_issue(10, labels=["approve-policy"])
        agent._check_policy_approvals()

        agent.github.remove_label.assert_called_once_with(10, "approve-policy")

    def test_skips_policy_without_source_task(self, tmp_path) -> None:
        agent = _make_agent(tmp_path)
        store = PolicyStore(agent.config.policy_db)
        _insert_draft(store, source_task=None)
        store.close()

        agent._check_policy_approvals()
        agent.github.get_issue.assert_not_called()

    def test_handles_multiple_policies_independently(self, tmp_path) -> None:
        agent = _make_agent(tmp_path)
        store = PolicyStore(agent.config.policy_db)
        pid_approve = _insert_draft(store, title="Approve me", source_task="10")
        pid_skip = _insert_draft(store, title="Skip me", source_task="20")
        store.close()

        def get_issue_side_effect(num: int) -> Issue:
            if num == 10:
                return _make_issue(10, labels=["approve-policy"])
            return _make_issue(20, labels=[])

        agent.github.get_issue.side_effect = get_issue_side_effect
        agent._check_policy_approvals()

        store2 = PolicyStore(agent.config.policy_db)
        assert store2.get(pid_approve).status == STATUS_ACTIVE
        assert store2.get(pid_skip).status == STATUS_DRAFT
        store2.close()

    def test_store_closed_after_check(self, tmp_path) -> None:
        agent = _make_agent(tmp_path)
        PolicyStore(agent.config.policy_db).close()

        closed: list = []
        original_close = PolicyStore.close

        def tracking_close(self_store: PolicyStore) -> None:
            closed.append(True)
            original_close(self_store)

        with patch.object(PolicyStore, "close", tracking_close):
            agent._check_policy_approvals()

        assert len(closed) == 1

    def test_skips_policy_when_issue_not_found(self, tmp_path) -> None:
        agent = _make_agent(tmp_path)
        store = PolicyStore(agent.config.policy_db)
        _insert_draft(store, source_task="999")
        store.close()

        agent.github.get_issue.return_value = None
        agent._check_policy_approvals()

        agent.github.comment_issue.assert_not_called()
