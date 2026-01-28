#!/usr/bin/env python3
"""
Cross-platform launcher for workflow engine agents.

Launches Planner, Worker, and Reviewer agents in parallel.
Works on both Windows and Linux/macOS.

Usage:
    python launch.py <owner/repo> [--mode MODE] [--config CONFIG]

Modes:
    auto     - Automatically choose best mode for platform (default)
    subprocess - Launch as subprocesses (cross-platform)
    tmux     - Launch in tmux (Linux/macOS only)
    terminal - Launch in Windows Terminal tabs (Windows only)
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
import signal
from pathlib import Path
from typing import List, Optional


class WorkflowLauncher:
    """Cross-platform launcher for workflow agents."""

    def __init__(self, repo: str, config: Optional[str] = None):
        self.repo = repo
        self.config = config
        self.script_dir = Path(__file__).parent
        self.engine_dir = self.script_dir.parent
        self.is_windows = platform.system() == "Windows"
        self.processes: List[subprocess.Popen] = []

    def _build_command(self, agent: str) -> List[str]:
        """Build command for an agent."""
        agent_path = self.engine_dir / f"{agent}-agent" / "main.py"
        cmd = ["uv", "run", str(agent_path), self.repo]
        if self.config:
            cmd.extend(["--config", self.config])
        return cmd

    def launch_subprocess(self) -> None:
        """Launch agents as subprocesses."""
        print("=" * 50)
        print("  Workflow Engine Launcher (subprocess mode)")
        print("=" * 50)
        print(f"\nRepository: {self.repo}")
        print("")

        # Register signal handler for cleanup
        signal.signal(signal.SIGINT, self._signal_handler)
        if not self.is_windows:
            signal.signal(signal.SIGTERM, self._signal_handler)

        # Start Worker and Reviewer as background processes
        print("Starting Worker Agent (background)...")
        worker_proc = subprocess.Popen(
            self._build_command("worker"),
            cwd=self.engine_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.processes.append(worker_proc)
        print(f"  PID: {worker_proc.pid}")

        print("Starting Reviewer Agent (background)...")
        reviewer_proc = subprocess.Popen(
            self._build_command("reviewer"),
            cwd=self.engine_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.processes.append(reviewer_proc)
        print(f"  PID: {reviewer_proc.pid}")

        print("")
        print("Starting Planner Agent (interactive)...")
        print("-" * 50)
        print("")

        # Run Planner in foreground
        try:
            planner_proc = subprocess.run(
                self._build_command("planner"),
                cwd=self.engine_dir,
            )
        except KeyboardInterrupt:
            pass
        finally:
            self._cleanup()

    def launch_tmux(self) -> None:
        """Launch agents in tmux session."""
        if self.is_windows:
            print("tmux is not available on Windows. Use 'subprocess' or 'terminal' mode.")
            sys.exit(1)

        if not shutil.which("tmux"):
            print("tmux not found. Install it or use 'subprocess' mode.")
            sys.exit(1)

        session_name = f"workflow-{self.repo.replace('/', '-')}"

        print("=" * 50)
        print("  Workflow Engine Launcher (tmux mode)")
        print("=" * 50)
        print(f"\nRepository: {self.repo}")
        print(f"Session: {session_name}")
        print("")

        # Kill existing session
        subprocess.run(
            ["tmux", "kill-session", "-t", session_name],
            capture_output=True,
        )

        worker_cmd = " ".join(self._build_command("worker"))
        reviewer_cmd = " ".join(self._build_command("reviewer"))
        planner_cmd = " ".join(self._build_command("planner"))

        # Create tmux session
        subprocess.run([
            "tmux", "new-session", "-d", "-s", session_name,
            "-n", "agents", "-c", str(self.engine_dir),
        ])

        # Send commands to panes
        subprocess.run([
            "tmux", "send-keys", "-t", session_name,
            f"echo '=== Worker Agent ===' && {worker_cmd}", "C-m",
        ])

        subprocess.run([
            "tmux", "split-window", "-h", "-t", session_name,
            "-c", str(self.engine_dir),
        ])
        subprocess.run([
            "tmux", "send-keys", "-t", session_name,
            f"echo '=== Reviewer Agent ===' && {reviewer_cmd}", "C-m",
        ])

        subprocess.run([
            "tmux", "select-pane", "-t", f"{session_name}:0.0",
        ])
        subprocess.run([
            "tmux", "split-window", "-v", "-t", session_name,
            "-c", str(self.engine_dir),
        ])
        subprocess.run([
            "tmux", "send-keys", "-t", session_name,
            f"echo '=== Planner Agent ===' && {planner_cmd}", "C-m",
        ])

        subprocess.run(["tmux", "select-layout", "-t", session_name, "main-vertical"])

        print("Attaching to tmux session...")
        print("Use 'Ctrl+B D' to detach")
        print("")

        subprocess.run(["tmux", "attach-session", "-t", session_name])

    def launch_terminal(self) -> None:
        """Launch agents in Windows Terminal tabs."""
        if not self.is_windows:
            print("Windows Terminal is only available on Windows. Use 'tmux' mode.")
            sys.exit(1)

        if not shutil.which("wt"):
            print("Windows Terminal (wt) not found. Use 'subprocess' mode.")
            sys.exit(1)

        print("=" * 50)
        print("  Workflow Engine Launcher (Windows Terminal)")
        print("=" * 50)
        print(f"\nRepository: {self.repo}")
        print("")

        worker_cmd = " ".join(self._build_command("worker"))
        reviewer_cmd = " ".join(self._build_command("reviewer"))
        planner_cmd = " ".join(self._build_command("planner"))

        # Build Windows Terminal command
        wt_cmd = [
            "wt",
            "--title", "Worker Agent",
            "-d", str(self.engine_dir),
            "cmd", "/k", worker_cmd,
            ";",
            "new-tab", "--title", "Reviewer Agent",
            "-d", str(self.engine_dir),
            "cmd", "/k", reviewer_cmd,
            ";",
            "new-tab", "--title", "Planner Agent",
            "-d", str(self.engine_dir),
            "cmd", "/k", planner_cmd,
        ]

        subprocess.run(wt_cmd)
        print("Windows Terminal launched with three tabs.")

    def launch_auto(self) -> None:
        """Automatically choose best launch mode."""
        if self.is_windows:
            if shutil.which("wt"):
                self.launch_terminal()
            else:
                self.launch_subprocess()
        else:
            if shutil.which("tmux"):
                self.launch_tmux()
            else:
                self.launch_subprocess()

    def _signal_handler(self, signum, frame) -> None:
        """Handle interrupt signals."""
        print("\n\nReceived interrupt signal. Cleaning up...")
        self._cleanup()
        sys.exit(0)

    def _cleanup(self) -> None:
        """Clean up background processes."""
        for proc in self.processes:
            if proc.poll() is None:  # Still running
                print(f"Stopping process {proc.pid}...")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


def main():
    parser = argparse.ArgumentParser(
        description="Cross-platform launcher for workflow engine agents"
    )
    parser.add_argument(
        "repo",
        help="Repository in owner/repo format",
    )
    parser.add_argument(
        "--mode", "-m",
        choices=["auto", "subprocess", "tmux", "terminal"],
        default="auto",
        help="Launch mode (default: auto)",
    )
    parser.add_argument(
        "--config", "-c",
        help="Path to config file",
    )

    args = parser.parse_args()

    launcher = WorkflowLauncher(args.repo, args.config)

    if args.mode == "auto":
        launcher.launch_auto()
    elif args.mode == "subprocess":
        launcher.launch_subprocess()
    elif args.mode == "tmux":
        launcher.launch_tmux()
    elif args.mode == "terminal":
        launcher.launch_terminal()


if __name__ == "__main__":
    main()
