"""Issue #16 contract tests (TDD-first).

These tests intentionally describe behavior that does not exist yet.
They define the acceptance contract for reusable, multi-repo operations.
"""

from __future__ import annotations

import importlib.util
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

import shared.config as config_module

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_SCRIPT = REPO_ROOT / "scripts" / "setup-repository.sh"
REQUIRED_DOCS = [
    "docs/SETUP_GUIDE.md",
    "docs/ARCHITECTURE.md",
    "docs/CUSTOMIZATION.md",
    "docs/TROUBLESHOOTING.md",
    "docs/DISTRIBUTION_OPTIONS.md",
]


def _load_main_module(alias: str, rel_path: str) -> Any:
    """Load a script-like agent entrypoint module from repository path."""
    module_path = REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(alias, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Failed to load {rel_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


def _require_attr(obj: Any, name: str) -> Any:
    """Require a new attribute/API and fail with a clear TDD message if missing."""
    assert hasattr(obj, name), f"Expected `{name}` to be implemented for issue #16"
    return getattr(obj, name)


def test_setup_script_exists_and_has_core_contract_markers() -> None:
    """Setup script must exist and declare required operational behavior markers."""
    assert SETUP_SCRIPT.exists(), "scripts/setup-repository.sh must be added"

    mode = SETUP_SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, "setup script should be executable"

    content = SETUP_SCRIPT.read_text(encoding="utf-8")
    assert "set -euo pipefail" in content
    assert "--repo" in content
    assert "--dry-run" in content
    assert "gh auth status" in content
    assert "config/labels.yml" in content
    assert "templates/github" in content
    assert "AGENTS.md" in content


def test_setup_script_auth_failure_exits_non_zero_with_recovery_guidance(
    tmp_path: Path,
) -> None:
    """If `gh auth status` fails, script should fail and print rerun guidance."""
    assert SETUP_SCRIPT.exists(), "scripts/setup-repository.sh must be added"

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"${1:-}\" == \"auth\" && \"${2:-}\" == \"status\" ]]; then\n"
        "  echo 'gh auth status failed' >&2\n"
        "  exit 1\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"

    result = subprocess.run(
        ["bash", str(SETUP_SCRIPT), "--repo", "org/example"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode != 0
    output = f"{result.stdout}\n{result.stderr}".lower()
    assert "gh auth login" in output
    assert any(word in output for word in ["retry", "re-run", "再実行"])


def test_setup_script_dry_run_does_not_call_mutating_gh_api(tmp_path: Path) -> None:
    """`--dry-run` should avoid mutating GitHub API methods (POST/PUT/PATCH/DELETE)."""
    assert SETUP_SCRIPT.exists(), "scripts/setup-repository.sh must be added"

    calls_log = tmp_path / "gh_calls.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env bash\n"
        "echo \"$*\" >> \"$GH_CALLS_LOG\"\n"
        "if [[ \"${1:-}\" == \"auth\" && \"${2:-}\" == \"status\" ]]; then\n"
        "  exit 0\n"
        "fi\n"
        "if [[ \"${1:-}\" == \"repo\" && \"${2:-}\" == \"clone\" ]]; then\n"
        "  target=\"${4:-${3##*/}}\"\n"
        "  mkdir -p \"$target\"\n"
        "  exit 0\n"
        "fi\n"
        "if [[ \"${1:-}\" == \"api\" ]]; then\n"
        "  # Safe default for read/list API calls\n"
        "  echo '[]'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["GH_CALLS_LOG"] = str(calls_log)

    result = subprocess.run(
        ["bash", str(SETUP_SCRIPT), "--repo", "org/example", "--dry-run"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0
    output = f"{result.stdout}\n{result.stderr}".lower()
    assert "dry-run" in output

    calls = calls_log.read_text(encoding="utf-8") if calls_log.exists() else ""
    assert " --method POST " not in f" {calls} "
    assert " --method PUT " not in f" {calls} "
    assert " --method PATCH " not in f" {calls} "
    assert " --method DELETE " not in f" {calls} "


def test_repos_example_includes_single_and_multi_repo_examples_with_comments() -> None:
    """Config example must document both single and multi-repo usage clearly."""
    config_example = REPO_ROOT / "config" / "repos.yml.example"
    content = config_example.read_text(encoding="utf-8")

    assert "defaults:" in content
    assert "run_mode" in content
    assert "max_concurrency" in content
    assert "fail_fast" in content

    # Single + multi example hints should both exist.
    assert re.search(r"(?im)^\s*-\s*name:\s*[^\n]+", content)
    assert content.count("- name:") >= 2

    # Requirement asks for commented meaning of fields.
    assert "#" in content


def test_labels_config_exists_for_externalized_label_definitions() -> None:
    """Label definitions must be extracted to config/labels.yml for extensibility."""
    labels_path = REPO_ROOT / "config" / "labels.yml"
    assert labels_path.exists(), "config/labels.yml must be added"

    data = yaml.safe_load(labels_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert data, "labels.yml must not be empty"
    assert any(k in data for k in ["profiles", "default", "labels"])


def test_target_repos_csv_parser_handles_duplicates_empty_and_invalid() -> None:
    """TARGET_REPOS parser should normalize CSV and reject invalid repo formats."""
    parser = _require_attr(config_module, "parse_target_repos_csv")

    assert parser("org/repo-a, org/repo-b,org/repo-a,,") == [
        "org/repo-a",
        "org/repo-b",
    ]

    with pytest.raises(ValueError):
        parser("org/repo-a,invalid-format")


def test_runtime_settings_resolution_priority_cli_over_env_over_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runtime settings must resolve in priority: CLI > ENV > repos.yml."""
    resolver = _require_attr(config_module, "resolve_runtime_settings")

    config_path = tmp_path / "repos.yml"
    config_path.write_text(
        """
defaults:
  run_mode: daemon
  max_concurrency: 2
  fail_fast: false

repositories:
  - name: org/yaml-a
    enabled: true
  - name: org/yaml-b
    enabled: true
""",
        encoding="utf-8",
    )

    monkeypatch.setenv("TARGET_REPOS", "org/env-a,org/env-b")
    monkeypatch.setenv("RUN_MODE", "once")
    monkeypatch.setenv("MAX_CONCURRENCY", "5")
    monkeypatch.setenv("FAIL_FAST", "true")
    monkeypatch.setenv("GITHUB_TOKEN", "env-token")

    settings = resolver(cli_repo="org/cli-only", config_path=str(config_path))

    assert settings.target_repos == ["org/cli-only"]
    assert settings.run_mode == "once"
    assert settings.max_concurrency == 5
    assert settings.fail_fast is True
    assert settings.github_token == "env-token"


def test_runtime_settings_use_env_and_yaml_when_cli_repo_not_provided(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When CLI repo is omitted, TARGET_REPOS should override repos.yml targets."""
    resolver = _require_attr(config_module, "resolve_runtime_settings")

    config_path = tmp_path / "repos.yml"
    config_path.write_text(
        """
defaults:
  run_mode: daemon
  max_concurrency: 3
  fail_fast: false

repositories:
  - name: org/yaml-a
    enabled: true
  - name: org/yaml-disabled
    enabled: false
""",
        encoding="utf-8",
    )

    monkeypatch.setenv("TARGET_REPOS", "org/env-a,org/env-b")

    settings = resolver(cli_repo=None, config_path=str(config_path))
    assert settings.target_repos == ["org/env-a", "org/env-b"]


@pytest.mark.parametrize(
    ("alias", "module_path", "agent_class_name"),
    [
        ("issue16_planner_main", "planner-agent/main.py", "PlannerAgent"),
        ("issue16_worker_main", "worker-agent/main.py", "WorkerAgent"),
        ("issue16_reviewer_main", "reviewer-agent/main.py", "ReviewerAgent"),
    ],
)
def test_agent_entrypoint_is_backward_compatible_for_single_repo(
    alias: str,
    module_path: str,
    agent_class_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing single-repo invocation must remain compatible."""
    module = _load_main_module(alias, module_path)

    calls: list[str] = []

    class FakeAgent:
        def __init__(self, repo: str, config_path: str | None = None) -> None:
            calls.append(f"init:{repo}:{config_path}")

        def run_once(self) -> bool:
            calls.append("run_once")
            return True

        def run(self) -> None:
            calls.append("run")

    monkeypatch.setattr(module, agent_class_name, FakeAgent)
    monkeypatch.setattr(sys, "argv", ["main.py", "org/legacy-repo", "--once"])

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert exc.value.code == 0
    assert calls[0].startswith("init:org/legacy-repo:")
    assert "run_once" in calls


@pytest.mark.parametrize(
    ("alias", "module_path", "agent_class_name"),
    [
        ("issue16_planner_env_main", "planner-agent/main.py", "PlannerAgent"),
        ("issue16_worker_env_main", "worker-agent/main.py", "WorkerAgent"),
        ("issue16_reviewer_env_main", "reviewer-agent/main.py", "ReviewerAgent"),
    ],
)
def test_agent_entrypoint_supports_target_repos_fail_fast_true(
    alias: str,
    module_path: str,
    agent_class_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With FAIL_FAST=true, processing must stop at first failed repository."""
    module = _load_main_module(alias, module_path)

    processed: list[str] = []

    class FakeAgent:
        def __init__(self, repo: str, config_path: str | None = None) -> None:
            self.repo = repo

        def run_once(self) -> bool:
            processed.append(self.repo)
            return self.repo != "org/fail"

        def run(self) -> None:
            processed.append(self.repo)

    monkeypatch.setattr(module, agent_class_name, FakeAgent)
    monkeypatch.setenv("TARGET_REPOS", "org/ok-a,org/fail,org/ok-b")
    monkeypatch.setenv("FAIL_FAST", "true")
    monkeypatch.setattr(sys, "argv", ["main.py", "--once"])

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert exc.value.code == 1
    assert processed == ["org/ok-a", "org/fail"]


@pytest.mark.parametrize(
    ("alias", "module_path", "agent_class_name"),
    [
        (
            "issue16_planner_continue_main",
            "planner-agent/main.py",
            "PlannerAgent",
        ),
        ("issue16_worker_continue_main", "worker-agent/main.py", "WorkerAgent"),
        (
            "issue16_reviewer_continue_main",
            "reviewer-agent/main.py",
            "ReviewerAgent",
        ),
    ],
)
def test_agent_entrypoint_supports_target_repos_fail_fast_false_and_summary(
    alias: str,
    module_path: str,
    agent_class_name: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With FAIL_FAST=false, all repos run and summary must include failed repos."""
    module = _load_main_module(alias, module_path)

    processed: list[str] = []

    class FakeAgent:
        def __init__(self, repo: str, config_path: str | None = None) -> None:
            self.repo = repo

        def run_once(self) -> bool:
            processed.append(self.repo)
            return self.repo != "org/fail"

        def run(self) -> None:
            processed.append(self.repo)

    monkeypatch.setattr(module, agent_class_name, FakeAgent)
    monkeypatch.setenv("TARGET_REPOS", "org/ok-a,org/fail,org/ok-b")
    monkeypatch.setenv("FAIL_FAST", "false")
    monkeypatch.setattr(sys, "argv", ["main.py", "--once"])

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert exc.value.code == 1
    assert processed == ["org/ok-a", "org/fail", "org/ok-b"]

    output = capsys.readouterr()
    combined = f"{output.out}\n{output.err}".lower()
    assert "org/fail" in combined
    assert "failed" in combined


def test_required_docs_exist_and_are_linked_from_readme() -> None:
    """Required operations docs must exist and be discoverable from README links."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    for doc in REQUIRED_DOCS:
        assert (REPO_ROOT / doc).exists(), f"Missing required doc: {doc}"
        assert doc in readme, f"README must link {doc}"


def test_distribution_options_doc_contains_comparison_and_final_decision() -> None:
    """Distribution options doc must compare A/B/C and explain final adoption."""
    path = REPO_ROOT / "docs" / "DISTRIBUTION_OPTIONS.md"
    assert path.exists(), "docs/DISTRIBUTION_OPTIONS.md must be added"

    content = path.read_text(encoding="utf-8")
    normalized = content.lower()

    assert "案a" in content or "option a" in normalized or "a:" in normalized
    assert "案b" in content or "option b" in normalized or "b:" in normalized
    assert "案c" in content or "option c" in normalized or "c:" in normalized

    for axis in [
        "導入容易性",
        "更新容易性",
        "権限要件",
        "ci統合性",
        "保守コスト",
    ]:
        assert axis in content

    assert any(k in content for k in ["採用案", "adopted", "採用"])
    assert any(k in content for k in ["不採用理由", "not adopted", "却下"])


def test_templates_and_todo_demo_are_present_with_readme_instructions() -> None:
    """Templates and minimum todo demo should exist with executable instructions."""
    expected_paths = [
        "templates/github/PULL_REQUEST_TEMPLATE.md",
        "templates/AGENTS.md",
        "examples/todo-demo/README.md",
    ]

    for rel in expected_paths:
        assert (REPO_ROOT / rel).exists(), f"Missing required path: {rel}"

    issue_template_dir = REPO_ROOT / "templates" / "github" / "ISSUE_TEMPLATE"
    assert issue_template_dir.exists(), "templates/github/ISSUE_TEMPLATE must exist"
    assert any(issue_template_dir.iterdir()), "ISSUE_TEMPLATE directory must not be empty"

    demo_readme = (REPO_ROOT / "examples" / "todo-demo" / "README.md").read_text(
        encoding="utf-8"
    )
    assert any(k in demo_readme.lower() for k in ["expected", "期待", "ログ"])
    assert any(k in demo_readme.lower() for k in ["how to run", "手順", "run"])
