"""Action Pack schema and normalization helpers."""

from __future__ import annotations

from typing import Literal, TypedDict

EVIDENCE_MAX_CHARS = 1000
SCHEMA_VERSION = "1.0"

Phase = Literal["implement", "quality", "test", "ci", "finalize"]
Status = Literal["ok", "failed", "no_op", "partial"]


class TaskContext(TypedDict, total=False):
    """Task-level metadata."""

    repo: str
    issue_number: int
    pr_number: int
    attempt: int
    agent: str


class CheckResult(TypedDict):
    """Normalized check result."""

    name: str
    exit_code: int
    result: str
    error_type: str
    primary_message: str
    evidence: str


class Blocker(TypedDict, total=False):
    """Blocker detail captured during normalization."""

    type: str
    message: str
    check_name: str


class ActionItem(TypedDict):
    """Recommended next action."""

    id: str
    priority: str
    title: str
    command_or_step: str
    expected_outcome: str


class ActionPack(TypedDict):
    """Shared Action Pack payload."""

    schema_version: str
    task: TaskContext
    phase: Phase
    status: Status
    checks: list[CheckResult]
    blockers: list[Blocker]
    actions: list[ActionItem]
    summary: str


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _normalize_primary_message(output: object) -> str:
    text = str(output or "").strip()
    if not text:
        return "no output"
    first_line = text.splitlines()[0].strip()
    return first_line or "no output"


def _result_from_exit_code(exit_code: int) -> str:
    return "ok" if exit_code == 0 else "failed"


def _base_check(
    name: str,
    exit_code: int,
    output: object,
    error_type: str,
    max_evidence_chars: int,
) -> CheckResult:
    text_output = str(output or "")
    return {
        "name": name,
        "exit_code": int(exit_code),
        "result": _result_from_exit_code(int(exit_code)),
        "error_type": "none" if exit_code == 0 else error_type,
        "primary_message": _normalize_primary_message(output),
        "evidence": _truncate(text_output, max_evidence_chars),
    }


def _unknown_check(
    name: str, raw_output: object, max_evidence_chars: int, error: Exception
) -> CheckResult:
    return {
        "name": name,
        "exit_code": -1,
        "result": "unknown",
        "error_type": "parse_error",
        "primary_message": "failed to parse tool output",
        "evidence": _truncate(f"{raw_output}\n{error}", max_evidence_chars),
    }


def parse_ruff_output(
    raw_output: str,
    exit_code: int,
    max_evidence_chars: int = EVIDENCE_MAX_CHARS,
) -> CheckResult:
    """Normalize ruff output."""
    try:
        return _base_check("ruff", exit_code, raw_output, "lint", max_evidence_chars)
    except Exception as exc:
        return _unknown_check("ruff", raw_output, max_evidence_chars, exc)


def parse_mypy_output(
    raw_output: str,
    exit_code: int,
    max_evidence_chars: int = EVIDENCE_MAX_CHARS,
) -> CheckResult:
    """Normalize mypy output."""
    try:
        return _base_check("mypy", exit_code, raw_output, "type", max_evidence_chars)
    except Exception as exc:
        return _unknown_check("mypy", raw_output, max_evidence_chars, exc)


def parse_pytest_output(
    raw_output: str,
    exit_code: int,
    max_evidence_chars: int = EVIDENCE_MAX_CHARS,
) -> CheckResult:
    """Normalize pytest output."""
    try:
        return _base_check("pytest", exit_code, raw_output, "test", max_evidence_chars)
    except Exception as exc:
        return _unknown_check("pytest", raw_output, max_evidence_chars, exc)


def classify_commit_result(success: bool, output: str = "", error: str = "") -> Status:
    """Classify commit result, explicitly handling no-op commits."""
    if success:
        return "ok"
    merged = f"{output}\n{error}".lower()
    if "no changes to commit" in merged:
        return "no_op"
    return "failed"


def generate_actions(checks: list[CheckResult]) -> list[ActionItem]:
    """Generate deterministic next actions from error types."""
    error_types = sorted(
        {
            check.get("error_type", "unknown")
            for check in checks
            if check.get("result") != "ok"
        }
    )

    actions: list[ActionItem] = []
    for error_type in error_types:
        if error_type == "lint":
            actions.append(
                {
                    "id": "fix-lint",
                    "priority": "high",
                    "title": "Fix ruff lint errors",
                    "command_or_step": "uv run ruff check . --fix",
                    "expected_outcome": "ruff exits with 0",
                }
            )
        elif error_type == "type":
            actions.append(
                {
                    "id": "fix-types",
                    "priority": "high",
                    "title": "Fix mypy type errors",
                    "command_or_step": "修正対象の型注釈/戻り値/呼び出し整合性を修正し、`uv run mypy .` を再実行する",
                    "expected_outcome": "mypy exits with 0",
                }
            )
        elif error_type == "test":
            actions.append(
                {
                    "id": "rerun-tests",
                    "priority": "high",
                    "title": "Reproduce and fix failing tests",
                    "command_or_step": "uv run pytest -v --tb=short",
                    "expected_outcome": "failing tests become passing",
                }
            )
        elif error_type == "commit_no_changes":
            actions.append(
                {
                    "id": "verify-no-op",
                    "priority": "medium",
                    "title": "Re-validate no-op cause",
                    "command_or_step": "生成内容と既存コード差分を比較し、書き込み失敗がないか確認する",
                    "expected_outcome": "no-op が妥当か、または差分生成が必要か判断できる",
                }
            )
        elif error_type == "timeout":
            actions.append(
                {
                    "id": "investigate-timeout",
                    "priority": "medium",
                    "title": "Investigate command timeout",
                    "command_or_step": "タイムアウト発生箇所の再実行とボトルネック調査を行う",
                    "expected_outcome": "タイムアウト原因が特定される",
                }
            )
        else:
            actions.append(
                {
                    "id": f"investigate-{error_type}",
                    "priority": "medium",
                    "title": f"Investigate {error_type} failure",
                    "command_or_step": "evidence を確認して原因を特定する",
                    "expected_outcome": "再現手順と修正方針が定まる",
                }
            )

    return actions


def _build_summary(
    checks: list[CheckResult], blockers: list[Blocker], actions: list[ActionItem]
) -> str:
    failed_checks = [check for check in checks if check.get("result") != "ok"]
    failed_count = len(failed_checks) + len(blockers)
    main_cause = (
        failed_checks[0].get("error_type")
        if failed_checks
        else (blockers[0].get("type", "none") if blockers else "none")
    )
    impact = ",".join(sorted({check.get("name", "unknown") for check in failed_checks}))
    if not impact:
        impact = "none"
    next_action = actions[0]["title"] if actions else "none"
    return (
        f"失敗数: {failed_count} / 主因: {main_cause} / "
        f"影響範囲: {impact} / 推奨次アクション: {next_action}"
    )


def build_action_pack(
    task: TaskContext,
    phase: Phase,
    status: Status,
    checks: list[CheckResult],
    blockers: list[Blocker],
    actions: list[ActionItem] | None = None,
    summary: str | None = None,
    schema_version: str = SCHEMA_VERSION,
) -> ActionPack:
    """Build a validated Action Pack payload."""
    if schema_version != SCHEMA_VERSION:
        raise ValueError(f"Unsupported schema_version: {schema_version}")

    if phase not in {"implement", "quality", "test", "ci", "finalize"}:
        raise ValueError(f"Invalid phase: {phase}")
    if status not in {"ok", "failed", "no_op", "partial"}:
        raise ValueError(f"Invalid status: {status}")

    required_task_keys = {"repo", "issue_number", "attempt", "agent"}
    missing = required_task_keys - set(task.keys())
    if missing:
        raise ValueError(f"Missing task fields: {sorted(missing)}")

    normalized_checks: list[CheckResult] = []
    normalized_blockers = list(blockers)
    for check in checks:
        try:
            normalized_checks.append(
                {
                    "name": str(check["name"]),
                    "exit_code": int(check["exit_code"]),
                    "result": str(check["result"]),
                    "error_type": str(check["error_type"]),
                    "primary_message": str(check["primary_message"]),
                    "evidence": _truncate(str(check["evidence"]), EVIDENCE_MAX_CHARS),
                }
            )
        except Exception as exc:
            normalized_checks.append(
                {
                    "name": str(check.get("name", "unknown")),
                    "exit_code": int(check.get("exit_code", -1)),
                    "result": "unknown",
                    "error_type": "parse_error",
                    "primary_message": "failed to normalize check",
                    "evidence": _truncate(str(check), EVIDENCE_MAX_CHARS),
                }
            )
            normalized_blockers.append(
                {
                    "type": "parse_error",
                    "message": str(exc),
                    "check_name": str(check.get("name", "unknown")),
                }
            )

    resolved_actions = (
        list(actions) if actions is not None else generate_actions(normalized_checks)
    )
    resolved_summary = (
        summary
        if summary is not None
        else _build_summary(normalized_checks, normalized_blockers, resolved_actions)
    )

    return {
        "schema_version": schema_version,
        "task": task,
        "phase": phase,
        "status": status,
        "checks": normalized_checks,
        "blockers": normalized_blockers,
        "actions": resolved_actions,
        "summary": resolved_summary,
    }
