"""Tests for workflow_engine.cli safeguards."""

from __future__ import annotations

import io
import tempfile
from pathlib import Path

from workflow_engine import cli


def test_find_local_repo_root_detects_workflow_engine_project() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "pyproject.toml").write_text(
            """
[project]
name = "workflow-engine"
version = "0.1.0"
""".strip()
            + "\n",
            encoding="utf-8",
        )
        sub = root / "subdir"
        sub.mkdir()

        result = cli._find_local_repo_root(sub)

        assert result == root


def test_warn_if_execution_source_mismatch_outputs_warning(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "pyproject.toml").write_text(
            """
[project]
name = "workflow-engine"
version = "0.1.0"
""".strip()
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(root)

        stderr = io.StringIO()
        monkeypatch.setattr("sys.stderr", stderr)

        cli._warn_if_execution_source_mismatch(
            "worker",
            Path(
                "/opt/pipx/venvs/workflow-engine/lib/python3.11/site-packages/worker-agent/main.py"
            ),
        )

        output = stderr.getvalue()
        assert "execution source mismatch detected" in output
        assert "pipx install . --force" in output
