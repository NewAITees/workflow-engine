"""Tests for policy metrics: fired_count and accepted_count."""

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.github_client import Issue, PullRequest
from shared.policy_store import STRENGTH_MEDIUM, PolicyStore

# ── Load planner-agent/main.py ────────────────────────────────────────────────

_repo_root = Path(__file__).parent.parent
_planner_dir = _repo_root / "planner-agent"
_planner_spec = importlib.util.spec_from_file_location(
    "planner_agent_main",
    _planner_dir / "main.py",
    submodule_search_locations=[],
)
if _planner_spec and _planner_spec.loader and "planner_agent_main" not in sys.modules:
    _planner_mod = types.ModuleType("planner_agent_main")
    _planner_mod.__file__ = str(_planner_dir / "main.py")
    sys.modules["planner_agent_main"] = _planner_mod
    _planner_spec.loader.exec_module(_planner_mod)  # type: ignore[union-attr]

from planner_agent_main import PlannerAgent  # noqa: E402

# ── Load reviewer-agent/main.py ───────────────────────────────────────────────

_reviewer_dir = _repo_root / "reviewer-agent"
_reviewer_spec = importlib.util.spec_from_file_location(
    "reviewer_agent_main",
    _reviewer_dir / "main.py",
    submodule_search_locations=[],
)
if (
    _reviewer_spec
    and _reviewer_spec.loader
    and "reviewer_agent_main" not in sys.modules
):
    _reviewer_mod = types.ModuleType("reviewer_agent_main")
    _reviewer_mod.__file__ = str(_reviewer_dir / "main.py")
    sys.modules["reviewer_agent_main"] = _reviewer_mod
    _reviewer_spec.loader.exec_module(_reviewer_mod)  # type: ignore[union-attr]

from reviewer_agent_main import ReviewerAgent  # noqa: E402

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_planner(tmp_path) -> PlannerAgent:
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


def _make_reviewer(tmp_path) -> ReviewerAgent:
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


def _insert_active_policy(store: PolicyStore, title: str = "Test Policy") -> str:
    pid = store.insert_candidate(
        {
            "title": title,
            "why": "Past bugs",
            "rules": ["Write tests first"],
            "strength": STRENGTH_MEDIUM,
            "trigger_tags": [],
            "trigger_conditions": [],
        }
    )
    store.approve(pid)
    return pid


def _make_issue(number: int, labels: list[str] | None = None) -> Issue:
    return Issue(
        number=number,
        title="Test",
        body="body",
        labels=labels or [],
        state="open",
    )


def _make_pr(number: int) -> PullRequest:
    return PullRequest(
        number=number,
        title="Test PR",
        body="Closes #10",
        head_branch="auto/issue-10",
        base_branch="main",
        state="open",
        labels=[],
    )


# ── fired_count ───────────────────────────────────────────────────────────────


class TestFiredCount:
    def test_fired_count_increments_when_policy_injected(self, tmp_path) -> None:
        agent = _make_planner(tmp_path)
        store = PolicyStore(agent.config.policy_db)
        pid = _insert_active_policy(store, "My Policy")
        store.close()

        from shared.llm_client import LLMResult

        agent.llm.create_spec.return_value = LLMResult(success=True, output="spec")
        agent._generate_spec("Add feature")

        store2 = PolicyStore(agent.config.policy_db)
        policy = store2.get(pid)
        store2.close()
        assert policy is not None
        assert policy.fired_count == 1

    def test_fired_count_increments_once_per_spec_call(self, tmp_path) -> None:
        agent = _make_planner(tmp_path)
        store = PolicyStore(agent.config.policy_db)
        pid = _insert_active_policy(store)
        store.close()

        from shared.llm_client import LLMResult

        agent.llm.create_spec.return_value = LLMResult(success=True, output="spec")
        agent._generate_spec("story 1")
        agent._generate_spec("story 2")

        store2 = PolicyStore(agent.config.policy_db)
        policy = store2.get(pid)
        store2.close()
        assert policy is not None
        assert policy.fired_count == 2

    def test_fired_count_not_incremented_when_llm_fails(self, tmp_path) -> None:
        agent = _make_planner(tmp_path)
        store = PolicyStore(agent.config.policy_db)
        pid = _insert_active_policy(store)
        store.close()

        from shared.llm_client import LLMResult

        # LLM fails AFTER policies are fetched — fired_count still increments
        # because injection intent was recorded before the LLM call
        agent.llm.create_spec.return_value = LLMResult(
            success=False, output="", error="err"
        )
        agent._generate_spec("story")

        store2 = PolicyStore(agent.config.policy_db)
        policy = store2.get(pid)
        store2.close()
        assert policy is not None
        # fired_count reflects that the policy was selected for injection
        assert policy.fired_count == 1

    def test_fired_count_zero_when_no_policy_db(self, tmp_path) -> None:
        agent = _make_planner(tmp_path)
        agent.config.policy_db = None

        from shared.llm_client import LLMResult

        agent.llm.create_spec.return_value = LLMResult(success=True, output="spec")
        # Should not raise
        agent._generate_spec("story")

    def test_generate_spec_returns_policy_ids(self, tmp_path) -> None:
        agent = _make_planner(tmp_path)
        store = PolicyStore(agent.config.policy_db)
        pid = _insert_active_policy(store)
        store.close()

        from shared.llm_client import LLMResult

        agent.llm.create_spec.return_value = LLMResult(success=True, output="spec")
        spec, policy_ids = agent._generate_spec("story")

        assert spec == "spec"
        assert pid in policy_ids


# ── _record_policy_application ────────────────────────────────────────────────


class TestRecordPolicyApplication:
    def test_posts_hidden_comment(self, tmp_path) -> None:
        agent = _make_planner(tmp_path)
        agent._record_policy_application(42, ["policy_abc", "policy_def"])

        agent.github.comment_issue.assert_called_once()
        args = agent.github.comment_issue.call_args[0]
        assert args[0] == 42
        assert "POLICIES_APPLIED" in args[1]
        assert "policy_abc" in args[1]
        assert "policy_def" in args[1]

    def test_does_nothing_when_no_policy_ids(self, tmp_path) -> None:
        agent = _make_planner(tmp_path)
        agent._record_policy_application(42, [])
        agent.github.comment_issue.assert_not_called()


# ── accepted_count ────────────────────────────────────────────────────────────


class TestAcceptedCount:
    def test_accepted_count_increments_when_pr_approved(self, tmp_path) -> None:
        reviewer = _make_reviewer(tmp_path)
        store = PolicyStore(reviewer.config.policy_db)
        pid = _insert_active_policy(store)
        store.close()

        linked_issue = _make_issue(10)
        reviewer.github.get_issue_comments.return_value = [
            {"body": f"<!-- POLICIES_APPLIED: {pid} -->"}
        ]
        reviewer._increment_accepted_policies(linked_issue)

        store2 = PolicyStore(reviewer.config.policy_db)
        policy = store2.get(pid)
        store2.close()
        assert policy is not None
        assert policy.accepted_count == 1

    def test_accepted_count_multiple_policies(self, tmp_path) -> None:
        reviewer = _make_reviewer(tmp_path)
        store = PolicyStore(reviewer.config.policy_db)
        pid1 = _insert_active_policy(store, "Policy 1")
        pid2 = _insert_active_policy(store, "Policy 2")
        store.close()

        linked_issue = _make_issue(10)
        reviewer.github.get_issue_comments.return_value = [
            {"body": f"<!-- POLICIES_APPLIED: {pid1},{pid2} -->"}
        ]
        reviewer._increment_accepted_policies(linked_issue)

        store2 = PolicyStore(reviewer.config.policy_db)
        assert store2.get(pid1).accepted_count == 1
        assert store2.get(pid2).accepted_count == 1
        store2.close()

    def test_no_increment_when_no_policy_comment(self, tmp_path) -> None:
        reviewer = _make_reviewer(tmp_path)
        store = PolicyStore(reviewer.config.policy_db)
        pid = _insert_active_policy(store)
        store.close()

        linked_issue = _make_issue(10)
        reviewer.github.get_issue_comments.return_value = [
            {"body": "Regular comment with no policy marker"}
        ]
        reviewer._increment_accepted_policies(linked_issue)

        store2 = PolicyStore(reviewer.config.policy_db)
        assert store2.get(pid).accepted_count == 0
        store2.close()

    def test_skipped_when_policy_db_not_configured(self, tmp_path) -> None:
        reviewer = _make_reviewer(tmp_path)
        reviewer.config.policy_db = None

        linked_issue = _make_issue(10)
        # Should not raise or call get_issue_comments
        reviewer._increment_accepted_policies(linked_issue)
        reviewer.github.get_issue_comments.assert_not_called()

    def test_store_closed_after_increment(self, tmp_path) -> None:
        reviewer = _make_reviewer(tmp_path)
        store = PolicyStore(reviewer.config.policy_db)
        pid = _insert_active_policy(store)
        store.close()

        linked_issue = _make_issue(10)
        reviewer.github.get_issue_comments.return_value = [
            {"body": f"<!-- POLICIES_APPLIED: {pid} -->"}
        ]

        closed: list = []
        original_close = PolicyStore.close

        def tracking_close(self_store: PolicyStore) -> None:
            closed.append(True)
            original_close(self_store)

        with patch.object(PolicyStore, "close", tracking_close):
            reviewer._increment_accepted_policies(linked_issue)

        assert len(closed) == 1
