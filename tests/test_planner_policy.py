"""Tests for policy injection in planner-agent and shared/llm_client.py."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

# planner-agent/main.py is loaded via conftest as planner_agent_main
import importlib.util
import types

from shared.llm_client import LLMClient, LLMResult
from shared.policy_store import STRENGTH_MEDIUM, PolicyStore

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


def _insert_active_policy(store: PolicyStore, title: str = "Read tests first") -> str:
    pid = store.insert_candidate(
        {
            "title": title,
            "why": "Past bugs from untested code",
            "rules": ["Write tests before implementation"],
            "strength": STRENGTH_MEDIUM,
            "trigger_tags": ["bugfix"],
            "trigger_conditions": [],
        }
    )
    store.approve(pid)
    return pid


# ── LLMClient.create_spec: policy injection ──────────────────────────────────


class TestCreateSpecPolicyInjection:
    def _make_llm(self) -> LLMClient:
        cfg = MagicMock()
        cfg.llm_backend = "codex"
        cfg.codex_cli = "codex"
        cfg.claude_cli = "claude"
        return LLMClient(cfg)

    def test_no_policies_prompt_has_no_policy_section(self) -> None:
        llm = self._make_llm()
        with patch.object(
            llm, "_run", return_value=LLMResult(success=True, output="spec")
        ) as mock_run:
            llm.create_spec("Add login feature")
            prompt = mock_run.call_args[0][0]
            assert "Applicable Policies" not in prompt

    def test_policies_added_to_prompt(self) -> None:
        llm = self._make_llm()
        policies = [
            {
                "title": "Read tests first",
                "why": "Past bugs",
                "rules": ["Check existing tests"],
            }
        ]
        with patch.object(
            llm, "_run", return_value=LLMResult(success=True, output="spec")
        ) as mock_run:
            llm.create_spec("Add login feature", policies=policies)
            prompt = mock_run.call_args[0][0]
            assert "Applicable Policies" in prompt
            assert "Read tests first" in prompt
            assert "Past bugs" in prompt
            assert "Check existing tests" in prompt

    def test_multiple_policies_all_injected(self) -> None:
        llm = self._make_llm()
        policies = [
            {"title": "Policy A", "why": "Reason A", "rules": ["Rule A1"]},
            {"title": "Policy B", "why": "Reason B", "rules": ["Rule B1", "Rule B2"]},
        ]
        with patch.object(
            llm, "_run", return_value=LLMResult(success=True, output="spec")
        ) as mock_run:
            llm.create_spec("story", policies=policies)
            prompt = mock_run.call_args[0][0]
            assert "Policy A" in prompt
            assert "Policy B" in prompt
            assert "Rule B2" in prompt

    def test_empty_policies_list_no_section(self) -> None:
        llm = self._make_llm()
        with patch.object(
            llm, "_run", return_value=LLMResult(success=True, output="spec")
        ) as mock_run:
            llm.create_spec("story", policies=[])
            prompt = mock_run.call_args[0][0]
            assert "Applicable Policies" not in prompt

    def test_user_story_still_in_prompt(self) -> None:
        llm = self._make_llm()
        policies = [{"title": "P", "why": "W", "rules": ["R"]}]
        with patch.object(
            llm, "_run", return_value=LLMResult(success=True, output="spec")
        ) as mock_run:
            llm.create_spec("Add search feature", policies=policies)
            prompt = mock_run.call_args[0][0]
            assert "Add search feature" in prompt


# ── PlannerAgent._get_policies_for_story ─────────────────────────────────────


class TestGetPoliciesForStory:
    def test_returns_empty_when_policy_db_not_configured(self, tmp_path) -> None:
        agent = _make_agent(tmp_path)
        agent.config.policy_db = None
        result = agent._get_policies_for_story("some story")
        assert result == []

    def test_returns_active_policies(self, tmp_path) -> None:
        agent = _make_agent(tmp_path)
        store = PolicyStore(agent.config.policy_db)
        try:
            _insert_active_policy(store, title="My Policy")
        finally:
            store.close()

        policies = agent._get_policies_for_story("fix a bug")
        assert any(p.title == "My Policy" for p in policies)

    def test_returns_empty_when_no_active_policies(self, tmp_path) -> None:
        agent = _make_agent(tmp_path)
        # Store exists but has no active policies
        store = PolicyStore(agent.config.policy_db)
        store.close()

        result = agent._get_policies_for_story("some story")
        assert result == []

    def test_store_closed_after_fetch(self, tmp_path) -> None:
        agent = _make_agent(tmp_path)
        closed: list = []
        original_close = PolicyStore.close

        def tracking_close(self_store: PolicyStore) -> None:
            closed.append(True)
            original_close(self_store)

        with patch.object(PolicyStore, "close", tracking_close):
            agent._get_policies_for_story("story")

        assert len(closed) == 1


# ── PlannerAgent._generate_spec: end-to-end injection ────────────────────────


class TestGenerateSpecWithPolicies:
    def test_policies_passed_to_create_spec(self, tmp_path) -> None:
        agent = _make_agent(tmp_path)
        store = PolicyStore(agent.config.policy_db)
        try:
            _insert_active_policy(store, title="Injected Policy")
        finally:
            store.close()

        agent.llm.create_spec.return_value = LLMResult(success=True, output="spec text")
        agent._generate_spec("Add feature")

        _, kwargs = agent.llm.create_spec.call_args
        policies_arg = kwargs.get("policies")
        assert policies_arg is not None
        assert any(p["title"] == "Injected Policy" for p in policies_arg)

    def test_no_policies_when_db_not_configured(self, tmp_path) -> None:
        agent = _make_agent(tmp_path)
        agent.config.policy_db = None
        agent.llm.create_spec.return_value = LLMResult(success=True, output="spec text")
        agent._generate_spec("Add feature")

        _, kwargs = agent.llm.create_spec.call_args
        assert not kwargs.get("policies")

    def test_returns_none_on_llm_failure(self, tmp_path) -> None:
        agent = _make_agent(tmp_path)
        agent.config.policy_db = None
        agent.llm.create_spec.return_value = LLMResult(
            success=False, output="", error="LLM error"
        )
        result = agent._generate_spec("story")
        assert result is None
