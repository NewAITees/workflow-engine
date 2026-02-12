"""Configuration management for workflow agents."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class AgentConfig:
    """Configuration for an agent."""

    repo: str
    poll_interval: int = 300  # 5 minutes
    work_dir: str | None = None
    llm_backend: str = "codex"  # "codex" or "claude"
    codex_cli: str = "codex"
    claude_cli: str = "claude"
    gh_cli: str = "gh"
    log_level: str = "INFO"
    auto_merge: bool = False  # Auto-merge approved PRs
    merge_method: str = "squash"  # "squash", "merge", or "rebase"
    stale_lock_timeout_minutes: int = 30  # Recover stale implementing locks

    def __post_init__(self) -> None:
        if self.work_dir is None:
            self.work_dir = str(Path.home() / ".workflow-engine" / "workspaces")
        else:
            # Expand ~ to home directory
            self.work_dir = str(Path(self.work_dir).expanduser())
        # Validate backend
        if self.llm_backend not in ("codex", "claude"):
            raise ValueError(
                f"Invalid llm_backend: {self.llm_backend}. Must be 'codex' or 'claude'"
            )
        # Validate merge_method
        if self.merge_method not in ("squash", "merge", "rebase"):
            raise ValueError(
                f"Invalid merge_method: {self.merge_method}. Must be 'squash', 'merge', or 'rebase'"
            )
        if self.stale_lock_timeout_minutes <= 0:
            raise ValueError("stale_lock_timeout_minutes must be a positive integer")


@dataclass
class RepoTarget:
    """Repository target metadata loaded from repos.yml."""

    name: str
    enabled: bool = True
    labels_profile: str | None = None


@dataclass
class WorkflowDefaults:
    """Top-level defaults in repos.yml."""

    run_mode: str = "once"
    max_concurrency: int = 1
    fail_fast: bool = False


@dataclass
class WorkflowConfig:
    """Full workflow configuration."""

    repositories: dict[str, AgentConfig] = field(default_factory=dict)
    targets: list[RepoTarget] = field(default_factory=list)
    defaults: WorkflowDefaults = field(default_factory=WorkflowDefaults)


@dataclass
class RuntimeSettings:
    """Runtime settings resolved from CLI, environment, and YAML."""

    target_repos: list[str]
    run_mode: str
    max_concurrency: int
    fail_fast: bool
    github_token: str | None


_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _is_valid_repo_name(value: str) -> bool:
    return bool(_REPO_PATTERN.match(value))


def _parse_bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value}")


def parse_target_repos_csv(csv_value: str) -> list[str]:
    """Parse and validate TARGET_REPOS CSV."""
    repos: list[str] = []
    seen: set[str] = set()
    for raw in csv_value.split(","):
        repo = raw.strip()
        if not repo:
            continue
        if not _is_valid_repo_name(repo):
            raise ValueError(f"Invalid repository format: {repo}")
        if repo not in seen:
            seen.add(repo)
            repos.append(repo)
    return repos


def load_config(config_path: str | None = None) -> WorkflowConfig:
    """Load configuration from YAML file."""
    if config_path is None:
        env_config = os.environ.get("WORKFLOW_CONFIG")
        if env_config:
            config_path = env_config
        else:
            cwd_config = Path.cwd() / "config" / "repos.yml"
            if cwd_config.exists():
                config_path = str(cwd_config)
            else:
                config_path = str(Path(__file__).parent.parent / "config" / "repos.yml")

    config_file = Path(config_path)
    if not config_file.exists():
        return WorkflowConfig()

    with open(config_file) as f:
        data = yaml.safe_load(f) or {}

    repos: dict[str, AgentConfig] = {}
    targets: list[RepoTarget] = []

    raw_defaults = data.get("defaults", {}) or {}
    run_mode = str(raw_defaults.get("run_mode", "once")).strip().lower()
    if run_mode not in {"once", "daemon"}:
        run_mode = "once"
    max_concurrency_raw = raw_defaults.get("max_concurrency", 1)
    try:
        max_concurrency = int(max_concurrency_raw)
    except (TypeError, ValueError):
        max_concurrency = 1
    if max_concurrency < 1:
        max_concurrency = 1
    fail_fast_raw = raw_defaults.get("fail_fast", False)
    if isinstance(fail_fast_raw, bool):
        fail_fast = fail_fast_raw
    else:
        try:
            fail_fast = _parse_bool(str(fail_fast_raw))
        except ValueError:
            fail_fast = False
    defaults = WorkflowDefaults(
        run_mode=run_mode,
        max_concurrency=max_concurrency,
        fail_fast=fail_fast,
    )

    for item in data.get("repositories", []) or []:
        repo_config = dict(item)
        name = str(repo_config.pop("name")).strip()
        if not _is_valid_repo_name(name):
            continue
        enabled = bool(repo_config.pop("enabled", True))
        labels_profile_raw = repo_config.pop("labels_profile", None)
        labels_profile = (
            str(labels_profile_raw).strip() if labels_profile_raw is not None else None
        )
        targets.append(
            RepoTarget(name=name, enabled=enabled, labels_profile=labels_profile)
        )
        repos[name] = AgentConfig(repo=name, **repo_config)

    return WorkflowConfig(repositories=repos, targets=targets, defaults=defaults)


def get_agent_config(repo: str, config_path: str | None = None) -> AgentConfig:
    """Get configuration for a specific repository."""
    config = load_config(config_path)

    if repo in config.repositories:
        return config.repositories[repo]

    # Return default config for unregistered repos
    return AgentConfig(repo=repo)


def resolve_runtime_settings(
    cli_repo: str | None,
    config_path: str | None = None,
    cli_run_mode: str | None = None,
) -> RuntimeSettings:
    """Resolve runtime settings by priority: CLI > ENV > YAML."""
    config = load_config(config_path)

    if cli_repo:
        if not _is_valid_repo_name(cli_repo):
            raise ValueError(f"Invalid repository format: {cli_repo}")
        target_repos = [cli_repo]
    else:
        env_targets = os.environ.get("TARGET_REPOS", "").strip()
        if env_targets:
            target_repos = parse_target_repos_csv(env_targets)
        else:
            target_repos = [t.name for t in config.targets if t.enabled]

    if not target_repos:
        raise ValueError("No target repositories resolved")

    run_mode = (
        (cli_run_mode or "").strip().lower()
        or os.environ.get("RUN_MODE", "").strip().lower()
        or config.defaults.run_mode
    )
    if run_mode not in {"once", "daemon"}:
        raise ValueError("RUN_MODE must be 'once' or 'daemon'")

    max_concurrency_raw = (
        os.environ.get("MAX_CONCURRENCY", "").strip() or config.defaults.max_concurrency
    )
    try:
        max_concurrency = int(max_concurrency_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("MAX_CONCURRENCY must be an integer") from exc
    if max_concurrency < 1:
        raise ValueError("MAX_CONCURRENCY must be >= 1")

    fail_fast_raw = os.environ.get("FAIL_FAST")
    if fail_fast_raw is None or fail_fast_raw.strip() == "":
        fail_fast = config.defaults.fail_fast
    else:
        fail_fast = _parse_bool(fail_fast_raw)

    return RuntimeSettings(
        target_repos=target_repos,
        run_mode=run_mode,
        max_concurrency=max_concurrency,
        fail_fast=fail_fast,
        github_token=os.environ.get("GITHUB_TOKEN"),
    )
