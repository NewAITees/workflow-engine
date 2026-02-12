"""Tests for Action Pack schema and normalization."""

from shared.action_pack import (
    build_action_pack,
    classify_commit_result,
    generate_actions,
    parse_mypy_output,
    parse_pytest_output,
    parse_ruff_output,
)


def test_build_action_pack_contains_required_fields() -> None:
    pack = build_action_pack(
        task={
            "repo": "owner/repo",
            "issue_number": 33,
            "attempt": 1,
            "agent": "worker",
        },
        phase="quality",
        status="failed",
        checks=[parse_ruff_output("app.py:1:1 F401", 1)],
        blockers=[],
    )
    assert set(
        [
            "schema_version",
            "task",
            "phase",
            "status",
            "checks",
            "blockers",
            "actions",
            "summary",
        ]
    ).issubset(set(pack.keys()))


def test_parsers_normalize_check_shape() -> None:
    ruff = parse_ruff_output("app.py:1:1 F401 unused", 1)
    mypy = parse_mypy_output("app.py:1: error: Incompatible types", 1)
    pytest = parse_pytest_output("FAILED tests/test_issue_33.py::test_x", 1)
    for check in [ruff, mypy, pytest]:
        assert {
            "name",
            "exit_code",
            "result",
            "error_type",
            "primary_message",
            "evidence",
        }.issubset(set(check.keys()))


def test_actions_and_summary_are_deterministic() -> None:
    check = parse_ruff_output("app.py:1:1 F401 unused", 1)
    pack1 = build_action_pack(
        task={
            "repo": "owner/repo",
            "issue_number": 33,
            "attempt": 1,
            "agent": "worker",
        },
        phase="quality",
        status="failed",
        checks=[check],
        blockers=[],
    )
    pack2 = build_action_pack(
        task={
            "repo": "owner/repo",
            "issue_number": 33,
            "attempt": 1,
            "agent": "worker",
        },
        phase="quality",
        status="failed",
        checks=[check],
        blockers=[],
    )
    assert pack1["summary"] == pack2["summary"]
    assert pack1["actions"] == pack2["actions"]


def test_commit_no_changes_is_no_op() -> None:
    assert classify_commit_result(False, "", "No changes to commit") == "no_op"


def test_generate_actions_for_lint_includes_fix_command() -> None:
    actions = generate_actions([parse_ruff_output("x.py:1:1 F401", 1)])
    assert any("ruff check . --fix" in action["command_or_step"] for action in actions)
