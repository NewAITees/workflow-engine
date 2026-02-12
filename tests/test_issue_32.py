"""TDD tests for issue #32: status watch dashboard enhancements."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


SPEC_PATH = Path(__file__).parent.parent / "scripts" / "status.py"


spec = importlib.util.spec_from_file_location("status_script", SPEC_PATH)
if spec is None or spec.loader is None:
    raise ImportError("Failed to load scripts/status.py")

status_script = importlib.util.module_from_spec(spec)
sys.modules["status_script"] = status_script
spec.loader.exec_module(status_script)


class TestIssue32WatchDashboard:
    """Tests that define expected watch-mode behavior for status.py."""

    def test_main_accepts_watch_and_interval_options(self, monkeypatch) -> None:
        """`status.py` should accept `--watch` and `--interval` CLI options."""
        monkeypatch.setattr(
            sys,
            "argv",
            ["status.py", "--watch", "--interval", "5", "--json"],
        )
        monkeypatch.setattr(
            status_script,
            "load_config",
            lambda _path: {"repositories": []},
        )

        status_script.main()

    def test_watch_mode_continuously_refreshes_until_interrupted(
        self, monkeypatch
    ) -> None:
        """Watch mode should fetch multiple refresh cycles until interrupted."""
        monkeypatch.setattr(
            sys,
            "argv",
            ["status.py", "--watch", "--interval", "1"],
        )
        monkeypatch.setattr(
            status_script,
            "load_config",
            lambda _path: {"repositories": [{"name": "owner/repo"}]},
        )

        run_gh = MagicMock(return_value=[])
        monkeypatch.setattr(status_script, "run_gh_command", run_gh)

        # Stop the watch loop deterministically after 2 wait points.
        sleep = MagicMock(side_effect=[None, KeyboardInterrupt])
        monkeypatch.setattr(
            status_script,
            "time",
            SimpleNamespace(sleep=sleep),
            raising=False,
        )

        with pytest.raises(KeyboardInterrupt):
            status_script.main()

        # Per cycle: issue list + pr list calls.
        assert run_gh.call_count >= 4

    def test_filter_items_by_issue_or_pr(self) -> None:
        """Target filter should narrow dashboard rows by issue/pr number."""
        assert hasattr(status_script, "filter_items_by_target")

        items = [
            {"type": "Issue", "number": 10, "title": "Issue 10"},
            {"type": "PR", "number": 11, "title": "PR 11"},
            {"type": "Issue", "number": 12, "title": "Issue 12"},
        ]

        issue_only = status_script.filter_items_by_target(items, issue_number=12)
        pr_only = status_script.filter_items_by_target(items, pr_number=11)

        assert issue_only == [{"type": "Issue", "number": 12, "title": "Issue 12"}]
        assert pr_only == [{"type": "PR", "number": 11, "title": "PR 11"}]

    def test_build_timeline_keeps_only_recent_n_events(self) -> None:
        """Timeline should be sorted by recency and truncated to N entries."""
        assert hasattr(status_script, "build_timeline")

        events = [
            {"id": "e1", "created_at": "2026-02-10T10:00:00Z"},
            {"id": "e2", "created_at": "2026-02-10T10:01:00Z"},
            {"id": "e3", "created_at": "2026-02-10T10:02:00Z"},
            {"id": "e4", "created_at": "2026-02-10T10:03:00Z"},
        ]

        timeline = status_script.build_timeline(events, limit=2)

        assert [entry["id"] for entry in timeline] == ["e4", "e3"]

    def test_compute_metrics_includes_duplicate_suspicions(self) -> None:
        """Metrics should include success rate, avg duration, retries, and duplicates."""
        assert hasattr(status_script, "compute_watch_metrics")

        runs = [
            {
                "target": "issue#1",
                "result": "success",
                "duration_seconds": 30,
                "retries": 0,
                "started_at": "2026-02-10T10:00:00Z",
            },
            {
                "target": "issue#1",
                "result": "failed",
                "duration_seconds": 20,
                "retries": 1,
                "started_at": "2026-02-10T10:01:00Z",
            },
            {
                "target": "pr#2",
                "result": "success",
                "duration_seconds": 10,
                "retries": 2,
                "started_at": "2026-02-10T10:02:00Z",
            },
            {
                "target": "pr#3",
                "result": "success",
                "duration_seconds": 40,
                "retries": 1,
                "started_at": "2026-02-10T10:03:00Z",
            },
        ]

        metrics = status_script.compute_watch_metrics(runs)

        assert metrics["success_rate"] == pytest.approx(0.75)
        assert metrics["avg_processing_time_seconds"] == pytest.approx(25.0)
        assert metrics["retry_distribution"] == {0: 1, 1: 2, 2: 1}
        assert metrics["duplicate_suspicions_count"] >= 1

    def test_duplicate_detection_flags_short_interval_repeats(self) -> None:
        """Duplicate suspicion detection should flag repeated short-interval runs."""
        assert hasattr(status_script, "detect_duplicate_executions")

        runs = [
            {"target": "issue#20", "started_at": "2026-02-10T10:00:00Z"},
            {"target": "issue#20", "started_at": "2026-02-10T10:01:30Z"},
            {"target": "issue#20", "started_at": "2026-02-10T10:15:00Z"},
            {"target": "pr#8", "started_at": "2026-02-10T10:03:00Z"},
        ]

        suspects = status_script.detect_duplicate_executions(runs, window_seconds=180)

        assert suspects == [{"target": "issue#20", "count": 2}]
