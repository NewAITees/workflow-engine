"""Issue #33 TDD tests for Action Pack normalization and no-op handling."""

import importlib
import importlib.util
import inspect
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from shared.github_client import Issue
from shared.llm_client import LLMResult

sys.path.insert(0, str(Path(__file__).parent.parent))


spec = importlib.util.spec_from_file_location(
    "planner_main", Path(__file__).parent.parent / "planner-agent" / "main.py"
)
if spec is None or spec.loader is None:
    raise ImportError("Failed to load planner-agent/main.py")

planner_main = importlib.util.module_from_spec(spec)
sys.modules["planner_main"] = planner_main
spec.loader.exec_module(planner_main)
PlannerAgent = planner_main.PlannerAgent

spec = importlib.util.spec_from_file_location(
    "worker_main", Path(__file__).parent.parent / "worker-agent" / "main.py"
)
if spec is None or spec.loader is None:
    raise ImportError("Failed to load worker-agent/main.py")

worker_main = importlib.util.module_from_spec(spec)
sys.modules["worker_main"] = worker_main
spec.loader.exec_module(worker_main)
WorkerAgent = worker_main.WorkerAgent


def _import_action_pack_module():
    """Import the issue #33 Action Pack module."""
    return importlib.import_module("shared.action_pack")


def _call_with_known_kwargs(func, raw_output: str, exit_code: int = 1):
    """Call parser-like functions with flexible keyword names."""
    sig = inspect.signature(func)
    kwargs = {}

    for name in sig.parameters:
        if name in {"raw_output", "output", "text", "stdout", "tool_output"}:
            kwargs[name] = raw_output
        elif name in {"exit_code", "returncode", "code"}:
            kwargs[name] = exit_code
        elif name in {"max_evidence_chars", "evidence_max_chars", "evidence_limit"}:
            kwargs[name] = 1000

    if kwargs:
        return func(**kwargs)

    if len(sig.parameters) >= 2:
        return func(raw_output, exit_code)
    return func(raw_output)


def _as_check(result):
    """Normalize parser output into a single check dictionary."""
    if isinstance(result, dict) and "checks" in result:
        checks = result["checks"]
        assert isinstance(checks, list) and checks
        return checks[0]
    if isinstance(result, list):
        assert result
        return result[0]
    assert isinstance(result, dict)
    return result


def _call_build_action_pack(module, **kwargs):
    """Call build_action_pack while tolerating minor signature differences."""
    build_action_pack = module.build_action_pack
    sig = inspect.signature(build_action_pack)
    accepted_kwargs = {
        name: value for name, value in kwargs.items() if name in sig.parameters
    }
    return build_action_pack(**accepted_kwargs)


def _call_classify_commit_result(module, *, success: bool, output: str, error: str):
    """Call classify_commit_result while tolerating signature differences."""
    classify = module.classify_commit_result
    sig = inspect.signature(classify)
    kwargs = {}
    for name in sig.parameters:
        if name in {"success", "ok"}:
            kwargs[name] = success
        elif name in {"output", "stdout", "message"}:
            kwargs[name] = output
        elif name in {"error", "stderr"}:
            kwargs[name] = error

    if kwargs:
        return classify(**kwargs)
    return classify(success, output, error)


def test_action_pack_schema_contains_required_top_level_fields():
    """Action Pack must include the required v1 schema fields."""
    action_pack = _import_action_pack_module()

    pack = _call_build_action_pack(
        action_pack,
        schema_version="1.0",
        task={
            "repo": "owner/repo",
            "issue_number": 33,
            "attempt": 1,
            "agent": "worker",
        },
        phase="quality",
        status="failed",
        checks=[
            {
                "name": "ruff",
                "exit_code": 1,
                "result": "failed",
                "error_type": "lint",
                "primary_message": "F401 imported but unused",
                "evidence": "worker-agent/main.py:1:1 F401",
            }
        ],
        blockers=[],
        actions=[],
        summary="1 failed check; lint error",
    )

    required = {
        "schema_version",
        "task",
        "phase",
        "status",
        "checks",
        "blockers",
        "actions",
        "summary",
    }
    assert required.issubset(set(pack.keys()))
    assert pack["schema_version"] == "1.0"
    assert {"repo", "issue_number", "attempt", "agent"}.issubset(
        set(pack["task"].keys())
    )


def test_action_pack_schema_rejects_invalid_phase_enum():
    """Invalid phase values should be rejected by schema validation."""
    action_pack = _import_action_pack_module()

    with pytest.raises((ValueError, AssertionError, KeyError, TypeError)):
        _call_build_action_pack(
            action_pack,
            schema_version="1.0",
            task={
                "repo": "owner/repo",
                "issue_number": 33,
                "attempt": 1,
                "agent": "worker",
            },
            phase="deploy",
            status="failed",
            checks=[],
            blockers=[],
            actions=[],
            summary="invalid",
        )


@pytest.mark.parametrize(
    ("parser_name", "sample_output", "exit_code"),
    [
        ("parse_ruff_output", "app.py:1:1: F401 unused import", 1),
        ("parse_mypy_output", "app.py:3: error: Incompatible types", 1),
        (
            "parse_pytest_output",
            "================= FAILURES =================\nE   AssertionError",
            1,
        ),
    ],
)
def test_tool_parsers_return_normalized_check_shape(
    parser_name: str, sample_output: str, exit_code: int
):
    """Each tool parser must produce check fields required by FR-2."""
    action_pack = _import_action_pack_module()
    parser = getattr(action_pack, parser_name)

    parsed = _call_with_known_kwargs(parser, sample_output, exit_code=exit_code)
    check = _as_check(parsed)

    assert {"name", "exit_code", "result", "error_type", "primary_message", "evidence"}.issubset(
        set(check.keys())
    )


def test_empty_tool_output_sets_no_output_message_and_truncates_evidence():
    """Empty output should map to 'no output' and evidence must be capped."""
    action_pack = _import_action_pack_module()

    parser = getattr(action_pack, "parse_pytest_output")
    parsed_empty = _call_with_known_kwargs(parser, "", exit_code=1)
    check_empty = _as_check(parsed_empty)
    assert check_empty["primary_message"] == "no output"

    long_output = "x" * 5000
    parsed_long = _call_with_known_kwargs(parser, long_output, exit_code=1)
    check_long = _as_check(parsed_long)
    assert len(check_long["evidence"]) <= 1000


def test_summary_and_actions_are_deterministic_for_same_failure_signature():
    """Same inputs must yield identical summary/actions across repeated runs."""
    action_pack = _import_action_pack_module()

    kwargs = dict(
        schema_version="1.0",
        task={
            "repo": "owner/repo",
            "issue_number": 33,
            "attempt": 2,
            "agent": "worker",
        },
        phase="quality",
        status="failed",
        checks=[
            {
                "name": "ruff",
                "exit_code": 1,
                "result": "failed",
                "error_type": "lint",
                "primary_message": "F401 imported but unused",
                "evidence": "worker-agent/main.py:4:1 F401",
            }
        ],
        blockers=[],
    )

    pack1 = _call_build_action_pack(action_pack, **kwargs)
    pack2 = _call_build_action_pack(action_pack, **kwargs)
    pack3 = _call_build_action_pack(action_pack, **kwargs)

    assert pack1["summary"] == pack2["summary"] == pack3["summary"]
    assert pack1["actions"] == pack2["actions"] == pack3["actions"]


def test_classify_commit_result_maps_no_changes_to_no_op():
    """Commit result 'No changes to commit' must be classified as no_op."""
    action_pack = _import_action_pack_module()

    classified = _call_classify_commit_result(
        action_pack,
        success=False,
        output="",
        error="No changes to commit",
    )

    if isinstance(classified, dict):
        status = classified.get("status")
    else:
        status = classified

    assert status == "no_op"


def test_generate_actions_for_lint_failure_contains_fix_command():
    """Lint error_type should produce an actionable ruff fix command."""
    action_pack = _import_action_pack_module()

    actions = action_pack.generate_actions(
        [
            {
                "name": "ruff",
                "exit_code": 1,
                "result": "failed",
                "error_type": "lint",
                "primary_message": "F401 imported but unused",
                "evidence": "app.py:1:1 F401",
            }
        ]
    )

    assert actions
    assert any("ruff check . --fix" in str(item.get("command_or_step", "")) for item in actions)


@patch("planner_main.LLMClient")
@patch("planner_main.GitHubClient")
@patch("planner_main.get_agent_config")
def test_planner_feedback_prefers_action_pack_over_raw_log_blob(
    mock_config, mock_github, mock_llm
):
    """Planner should prioritize Action Pack summary/blockers/actions for prompt input."""
    mock_config.return_value = MagicMock(
        gh_cli="gh", llm_backend="codex", poll_interval=30
    )
    mock_llm.return_value.create_spec.return_value = MagicMock(
        success=True, output="irrelevant"
    )

    agent = PlannerAgent("owner/repo")

    action_pack_payload = {
        "schema_version": "1.0",
        "task": {
            "repo": "owner/repo",
            "issue_number": 33,
            "attempt": 1,
            "agent": "worker",
        },
        "phase": "quality",
        "status": "failed",
        "checks": [
            {
                "name": "ruff",
                "exit_code": 1,
                "result": "failed",
                "error_type": "lint",
                "primary_message": "unused import",
                "evidence": "worker-agent/main.py:4:1 F401",
            }
        ],
        "blockers": [{"type": "lint", "message": "unused import"}],
        "actions": [
            {
                "id": "fix-lint",
                "priority": "high",
                "title": "Fix ruff lint errors",
                "command_or_step": "uv run ruff check . --fix",
                "expected_outcome": "ruff exits with 0",
            }
        ],
        "summary": "失敗数: 1 / 主因: lint / 影響範囲: worker-agent / 次アクション: ruff --fix",
    }

    comments = [
        {
            "created_at": "2026-02-10T10:00:00Z",
            "body": (
                "ESCALATION:worker\n"
                "Action Pack:\n"
                "```json\n"
                f"{json.dumps(action_pack_payload, ensure_ascii=False)}\n"
                "```\n\n"
                "RAW LOG (TRUNCATED SHOULD BE PREFERRED OVER FULL):\n"
                + ("trace line\n" * 800)
            ),
        }
    ]

    feedback = agent._collect_escalation_feedback(comments)

    assert "失敗数: 1" in feedback
    assert "Fix ruff lint errors" in feedback
    assert "trace line\ntrace line\ntrace line" not in feedback


@patch("shared.git_operations.GitOperations")
@patch("shared.llm_client.LLMClient")
@patch("shared.lock.LockManager")
@patch("shared.github_client.GitHubClient")
@patch("shared.config.get_agent_config")
def test_worker_no_op_commit_does_not_mark_failed_or_escalate(
    mock_config, mock_github, mock_lock, mock_llm, mock_git
):
    """No-op implementation commit should not be treated as a hard failure."""
    mock_config.return_value = MagicMock(
        work_dir="/tmp/test",
        llm_backend="codex",
        gh_cli="gh",
    )
    mock_git.return_value.workspace = "/tmp/test/workspace"

    agent = WorkerAgent("owner/repo")
    agent.lock.try_lock_issue = MagicMock(return_value=MagicMock(success=True))
    agent.lock.mark_failed = MagicMock()
    agent.lock.mark_needs_clarification = MagicMock()
    agent._comment_worker_escalation = MagicMock()

    issue_git = MagicMock()
    issue_git.path = Path("/tmp/test/repo")
    issue_git.commit.side_effect = [
        MagicMock(success=True, output="tests committed"),
        MagicMock(success=False, output="", error="No changes to commit"),
    ]

    agent._issue_workspace = MagicMock(return_value=nullcontext(issue_git))
    agent._snapshot_test_files = MagicMock(return_value=set())
    agent._ensure_issue_test_file = MagicMock(return_value=None)
    agent.llm.generate_tests = MagicMock(return_value=LLMResult(success=True, output="ok"))
    agent.llm.generate_implementation = MagicMock(
        return_value=LLMResult(success=True, output="ok")
    )
    agent.git.cleanup_branch = MagicMock()

    issue = Issue(number=33, title="No-op issue", body="Long enough spec " * 20, labels=[])

    agent._try_process_issue(issue)

    agent.lock.mark_failed.assert_not_called()
    agent._comment_worker_escalation.assert_not_called()
