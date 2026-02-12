#!/usr/bin/env python3
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.append(str(project_root))

from shared.console import (  # noqa: E402
    console,
    print_error,
    print_header,
    print_success,
    print_warning,
)


def _run_command(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def _check_cli_probe(cmd: str, probe_args: list[str]) -> dict[str, object]:
    path = shutil.which(cmd)
    if not path:
        return {
            "ok": False,
            "message": f"{cmd} command not found",
            "details": {"command": cmd},
        }

    try:
        _run_command([cmd, *probe_args])
    except subprocess.CalledProcessError as exc:
        error = (exc.stderr or exc.stdout or "command failed").strip()
        return {
            "ok": False,
            "message": f"{cmd} probe failed",
            "details": {"command": cmd, "error": error},
        }
    except Exception as exc:  # pragma: no cover - defensive catch
        return {
            "ok": False,
            "message": f"{cmd} probe failed",
            "details": {"command": cmd, "error": str(exc)},
        }

    return {
        "ok": True,
        "message": f"{cmd} probe succeeded",
        "details": {"command": cmd, "path": path},
    }


def check_llm_clis() -> dict[str, object]:
    checks = {
        "codex": _check_cli_probe("codex", ["--version"]),
        "claude": _check_cli_probe("claude", ["--version"]),
    }
    ok = all(bool(result["ok"]) for result in checks.values())
    return {
        "ok": ok,
        "message": "LLM CLI checks passed" if ok else "LLM CLI checks failed",
        "checks": checks,
    }


def check_github_api() -> dict[str, object]:
    if not shutil.which("gh"):
        return {
            "ok": False,
            "message": "gh command not found",
            "checks": {
                "rate_limit": {
                    "ok": False,
                    "message": "GitHub CLI command is not available",
                }
            },
        }

    try:
        result = _run_command(["gh", "api", "/rate_limit"])
        details: dict[str, object] = {}
        try:
            payload = json.loads(result.stdout)
            resources = payload.get("resources", {}) if isinstance(payload, dict) else {}
            core = resources.get("core", {}) if isinstance(resources, dict) else {}
            remaining = core.get("remaining") if isinstance(core, dict) else None
            details["remaining"] = remaining
        except json.JSONDecodeError:
            details["remaining"] = None

        return {
            "ok": True,
            "message": "GitHub API read-only check passed",
            "checks": {
                "rate_limit": {
                    "ok": True,
                    "message": "gh api /rate_limit succeeded",
                    "details": details,
                }
            },
        }
    except subprocess.CalledProcessError as exc:
        error = (exc.stderr or exc.stdout or "command failed").strip()
        return {
            "ok": False,
            "message": "GitHub API read-only check failed",
            "checks": {
                "rate_limit": {
                    "ok": False,
                    "message": "gh api /rate_limit failed",
                    "details": {"error": error},
                }
            },
        }


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if hasattr(sys, "platform") and sys.platform.startswith("win"):
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
            )
            return str(pid) in result.stdout
        return subprocess.run(["kill", "-0", str(pid)]).returncode == 0
    except Exception:
        return False


def _check_pid_files(log_dir: Path) -> dict[str, dict[str, object]]:
    mapping = {
        "planner": "planner.pid",
        "worker": "worker.pid",
        "reviewer": "reviewer.pid",
    }
    statuses: dict[str, dict[str, object]] = {}

    for agent, filename in mapping.items():
        pid_path = log_dir / filename
        if not pid_path.exists():
            statuses[agent] = {
                "ok": True,
                "status": "not_running",
                "source": "pid",
                "message": "pid file not found",
            }
            continue

        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except ValueError:
            statuses[agent] = {
                "ok": False,
                "status": "unknown",
                "source": "pid",
                "message": f"invalid pid file: {pid_path}",
            }
            continue

        if _pid_running(pid):
            statuses[agent] = {
                "ok": True,
                "status": "running",
                "source": "pid",
                "message": f"process running (pid={pid})",
            }
        else:
            statuses[agent] = {
                "ok": False,
                "status": "stale_pid",
                "source": "pid",
                "message": f"stale pid file (pid={pid})",
            }

    return statuses


def _check_tmux_sessions() -> dict[str, object]:
    if not shutil.which("tmux"):
        return {"available": False, "sessions": []}

    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            check=True,
            capture_output=True,
            text=True,
        )
        sessions = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return {"available": True, "sessions": sessions}
    except subprocess.CalledProcessError:
        return {"available": True, "sessions": []}


def _check_systemd_units() -> dict[str, object]:
    if not shutil.which("systemctl"):
        return {"available": False, "active_units": {}}

    units = [
        "workflow-planner.service",
        "workflow-worker.service",
        "workflow-reviewer.service",
    ]
    active_units: dict[str, bool] = {}

    for unit in units:
        result = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True,
            text=True,
        )
        active_units[unit] = result.returncode == 0 and result.stdout.strip() == "active"

    return {"available": True, "active_units": active_units}


def check_agents_runtime() -> dict[str, object]:
    log_dir = project_root / "logs"
    pid_status = _check_pid_files(log_dir)
    tmux_info = _check_tmux_sessions()
    systemd_info = _check_systemd_units()

    ok = all(bool(status["ok"]) for status in pid_status.values())

    return {
        "ok": ok,
        "message": "Agent runtime checks passed" if ok else "Agent runtime checks failed",
        "checks": pid_status,
        "tmux": tmux_info,
        "systemd": systemd_info,
    }


def check_config(config_path: Path) -> dict[str, object]:
    if not config_path.exists():
        return {"ok": False, "message": f"Config file missing: {config_path}"}

    try:
        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

        repos = config.get("repositories", [])
        if not isinstance(repos, list) or not repos:
            return {
                "ok": True,
                "message": "No repositories configured in repos.yml",
                "warning": True,
            }

        return {
            "ok": True,
            "message": f"Config loaded valid: {len(repos)} repositories configured",
        }
    except Exception as exc:
        return {"ok": False, "message": f"Config file invalid: {exc}"}


def run_health_checks() -> dict[str, object]:
    checks = {
        "llm": check_llm_clis(),
        "github": check_github_api(),
        "agents": check_agents_runtime(),
        "config": check_config(project_root / "config" / "repos.yml"),
    }
    overall_ok = all(bool(group["ok"]) for group in checks.values())
    return {
        "ok": overall_ok,
        "message": "All health checks passed" if overall_ok else "Health gate failed",
        "checks": checks,
    }


def _print_human(results: dict[str, object]) -> None:
    print_header("Workflow Engine Health Check")

    checks = results["checks"]

    console.rule("[bold]LLM CLI[/bold]")
    llm_checks = checks["llm"]["checks"]
    for name, result in llm_checks.items():
        if result["ok"]:
            print_success(result["message"])
        else:
            print_error(result["message"])
            details = result.get("details")
            if isinstance(details, dict) and details.get("error"):
                print_error(str(details["error"]))

    console.rule("[bold]GitHub API[/bold]")
    gh = checks["github"]["checks"]["rate_limit"]
    if gh["ok"]:
        print_success(gh["message"])
    else:
        print_error(gh["message"])
        details = gh.get("details")
        if isinstance(details, dict) and details.get("error"):
            print_error(str(details["error"]))

    console.rule("[bold]Agent Runtime[/bold]")
    for agent, status in checks["agents"]["checks"].items():
        label = f"{agent}: {status['message']}"
        if status["ok"]:
            print_success(label)
        else:
            print_error(label)

    console.rule("[bold]Configuration[/bold]")
    config_result = checks["config"]
    if config_result.get("ok"):
        if config_result.get("warning"):
            print_warning(str(config_result["message"]))
        else:
            print_success(str(config_result["message"]))
    else:
        print_error(str(config_result["message"]))

    console.rule("[bold]Summary[/bold]")
    if results["ok"]:
        console.print("[bold green]All systems go![/bold green]")
    else:
        console.print("[bold red]Health gate failed. Startup is blocked.[/bold red]")


def main() -> None:
    parser = argparse.ArgumentParser(description="Workflow Engine health check")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    results = run_health_checks()

    if args.json:
        print(json.dumps(results, ensure_ascii=False))
    else:
        _print_human(results)

    if not results["ok"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
