"""TDD tests for issue #31: pre-launch Health Gate."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).parent.parent


def _load_script_module(module_name: str, script_relative_path: str):
    script_path = REPO_ROOT / script_relative_path
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Failed to load {script_relative_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _cmd_text(cmd: Any) -> str:
    if isinstance(cmd, (list, tuple)):
        return " ".join(str(part) for part in cmd)
    return str(cmd)


def _extract_json(stdout: str) -> dict[str, Any]:
    return json.loads(stdout.strip())


def _success_run(cmd: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    text = _cmd_text(cmd)
    if "gh api" in text and "rate_limit" in text:
        return subprocess.CompletedProcess(cmd, 0, '{"resources": {"core": {"remaining": 99}}}', "")
    return subprocess.CompletedProcess(cmd, 0, "ok", "")


def test_health_check_supports_json_with_check_breakdown(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--json` should return machine-readable output including all check groups."""
    health_check = _load_script_module("health_check_issue31_json", "scripts/health_check.py")

    monkeypatch.setattr(health_check.shutil, "which", lambda _cmd: "/usr/bin/mock")
    monkeypatch.setattr(health_check.subprocess, "run", _success_run)
    monkeypatch.setattr(sys, "argv", ["health_check.py", "--json"])

    health_check.main()
    data = _extract_json(capsys.readouterr().out)

    serialized = json.dumps(data).lower()
    assert any(key in serialized for key in ["llm", "codex", "claude"])
    assert any(key in serialized for key in ["github", "rate_limit"])
    assert "agent" in serialized


def test_health_check_reports_llm_probe_failure_reason(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When LLM CLI probes fail, health check should fail with a cause-specific message."""
    health_check = _load_script_module("health_check_issue31_llm", "scripts/health_check.py")

    def failing_llm_run(cmd: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        text = _cmd_text(cmd)
        if text.startswith("codex") or text.startswith("claude"):
            raise subprocess.CalledProcessError(1, cmd, stderr="command failed")
        return _success_run(cmd, *args, **kwargs)

    monkeypatch.setattr(health_check.shutil, "which", lambda _cmd: "/usr/bin/mock")
    monkeypatch.setattr(health_check.subprocess, "run", failing_llm_run)
    monkeypatch.setattr(sys, "argv", ["health_check.py", "--json"])

    with pytest.raises(SystemExit) as exc:
        health_check.main()

    assert exc.value.code == 1
    output = capsys.readouterr().out.lower()
    assert "codex" in output or "claude" in output


def test_health_check_reports_github_api_failure_reason(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When GitHub read-only API check fails, health check should fail and explain why."""
    health_check = _load_script_module("health_check_issue31_gh", "scripts/health_check.py")

    def failing_gh_run(cmd: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        text = _cmd_text(cmd)
        if "gh api" in text and "rate_limit" in text:
            raise subprocess.CalledProcessError(1, cmd, stderr="rate limit check failed")
        return _success_run(cmd, *args, **kwargs)

    monkeypatch.setattr(health_check.shutil, "which", lambda _cmd: "/usr/bin/mock")
    monkeypatch.setattr(health_check.subprocess, "run", failing_gh_run)
    monkeypatch.setattr(sys, "argv", ["health_check.py", "--json"])

    with pytest.raises(SystemExit) as exc:
        health_check.main()

    assert exc.value.code == 1
    output = capsys.readouterr().out.lower()
    assert "github" in output or "rate_limit" in output or "gh api" in output


def test_health_check_json_includes_per_agent_runtime_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Health JSON should include runtime visibility for planner/worker/reviewer agents."""
    health_check = _load_script_module("health_check_issue31_agents", "scripts/health_check.py")

    monkeypatch.setattr(health_check.shutil, "which", lambda _cmd: "/usr/bin/mock")
    monkeypatch.setattr(health_check.subprocess, "run", _success_run)
    monkeypatch.setattr(sys, "argv", ["health_check.py", "--json"])

    health_check.main()
    data = _extract_json(capsys.readouterr().out)

    serialized = json.dumps(data).lower()
    assert "planner" in serialized
    assert "worker" in serialized
    assert "reviewer" in serialized


def test_launch_py_runs_health_gate_before_starting_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`scripts/launch.py` must invoke health gate first, before creating agent processes."""
    launch = _load_script_module("launch_issue31_order", "scripts/launch.py")

    events: list[str] = []

    class FakeProc:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def poll(self) -> int | None:
            return 0

        def terminate(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

    def fake_run(cmd: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        text = _cmd_text(cmd)
        if "health_check.py" in text:
            events.append("run:health")
        else:
            events.append("run:other")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def fake_popen(cmd: Any, *args: Any, **kwargs: Any) -> FakeProc:
        events.append("popen")
        return FakeProc(1234)

    monkeypatch.setattr(launch.subprocess, "run", fake_run)
    monkeypatch.setattr(launch.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(launch.signal, "signal", lambda *_args, **_kwargs: None)

    launcher = launch.WorkflowLauncher("owner/repo")
    launcher.launch_subprocess()

    assert "run:health" in events
    assert "popen" in events
    assert events.index("run:health") < events.index("popen")


def test_launch_py_blocks_startup_when_health_gate_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Launcher must not start Worker/Reviewer if health gate does not pass."""
    launch = _load_script_module("launch_issue31_block", "scripts/launch.py")

    popen_calls: list[Any] = []
    run_calls: list[str] = []

    def fake_run(cmd: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        text = _cmd_text(cmd)
        run_calls.append(text)
        if "health_check.py" in text:
            raise subprocess.CalledProcessError(1, cmd, stderr="health gate failed")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    class FakeProc:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def poll(self) -> int | None:
            return 0

        def terminate(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

    def fake_popen(cmd: Any, *args: Any, **kwargs: Any) -> FakeProc:
        popen_calls.append(cmd)
        return FakeProc(5678)

    monkeypatch.setattr(launch.subprocess, "run", fake_run)
    monkeypatch.setattr(launch.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(launch.signal, "signal", lambda *_args, **_kwargs: None)

    launcher = launch.WorkflowLauncher("owner/repo")

    with pytest.raises(SystemExit):
        launcher.launch_subprocess()

    assert any("health_check.py" in call for call in run_calls)
    assert popen_calls == []


def test_launch_sh_has_health_gate_wiring() -> None:
    """Bash launcher should call health check and stop on failure."""
    launch_sh = (REPO_ROOT / "scripts" / "launch.sh").read_text(encoding="utf-8")

    assert "health_check.py" in launch_sh
    assert "exit 1" in launch_sh


def test_launch_ps1_has_health_gate_wiring() -> None:
    """PowerShell launcher should call health check and stop on failure."""
    launch_ps1 = (REPO_ROOT / "scripts" / "launch.ps1").read_text(encoding="utf-8")

    assert "health_check.py" in launch_ps1
    assert "exit" in launch_ps1.lower()
