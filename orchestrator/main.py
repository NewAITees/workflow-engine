#!/usr/bin/env python3
"""Orchestrator - Claude-based supervisor for workflow agents.

Starts planner, worker, and reviewer as subprocesses and monitors
their health. Anomaly detection and intervention logic are added in
subsequent phases (O-2, O-3, O-4).
"""

import argparse
import logging
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Add parent directory to path for shared imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.intervention import InterventionService
from orchestrator.monitor import MonitorService
from shared.config import get_agent_config
from shared.github_client import GitHubClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("orchestrator")


@dataclass
class AgentProcess:
    """A managed subprocess for a single agent."""

    name: str
    cmd: list[str]
    process: subprocess.Popen | None = None
    restart_count: int = 0
    max_restarts: int = 5

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        logger.info(f"Starting {self.name}: {' '.join(self.cmd)}")
        self.process = subprocess.Popen(
            self.cmd,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )

    def stop(self) -> None:
        if self.process and self.is_alive():
            logger.info(f"Stopping {self.name} (pid={self.process.pid})")
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                logger.warning(f"{self.name} did not stop gracefully, killing")
                self.process.kill()
                self.process.wait()

    def restart(self) -> bool:
        """Restart the process. Returns False if max_restarts exceeded."""
        if self.restart_count >= self.max_restarts:
            logger.error(
                f"{self.name} exceeded max restarts ({self.max_restarts}), giving up"
            )
            return False
        self.stop()
        self.restart_count += 1
        logger.info(
            f"Restarting {self.name} (attempt {self.restart_count}/{self.max_restarts})"
        )
        self.start()
        return True


@dataclass
class Orchestrator:
    """Supervises planner, worker, and reviewer agents."""

    repo: str
    check_interval: int = 60  # seconds between health checks
    agents: list[AgentProcess] = field(default_factory=list)
    _running: bool = field(default=False, init=False)
    _monitor: MonitorService = field(init=False)
    _intervention: InterventionService = field(init=False)

    def __post_init__(self) -> None:
        uv = self._find_uv()
        root = str(Path(__file__).parent.parent)

        github = GitHubClient(self.repo)
        self._monitor = MonitorService(github)
        self._intervention = InterventionService(github)

        self.agents = [
            AgentProcess(
                name="planner",
                cmd=[uv, "run", f"{root}/planner-agent/main.py", self.repo, "--daemon"],
            ),
            AgentProcess(
                name="worker",
                cmd=[uv, "run", f"{root}/worker-agent/main.py", self.repo],
            ),
            AgentProcess(
                name="reviewer",
                cmd=[uv, "run", f"{root}/reviewer-agent/main.py", self.repo],
            ),
        ]

    def _find_uv(self) -> str:
        """Locate the uv executable."""
        candidates = [
            Path.home() / ".local" / "bin" / "uv",
            Path("/usr/local/bin/uv"),
        ]
        for p in candidates:
            if p.exists():
                return str(p)
        return "uv"

    def start(self) -> None:
        """Start all agents and enter the monitoring loop."""
        self._running = True
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        logger.info(f"Orchestrator starting for {self.repo}")
        for agent in self.agents:
            agent.start()
            time.sleep(1)  # stagger starts

        self._monitor_loop()

    def _monitor_loop(self) -> None:
        """Periodically check agent health and restart crashed processes."""
        while self._running:
            time.sleep(self.check_interval)
            if not self._running:
                break
            self._check_agent_health()

    def _check_agent_health(self) -> None:
        """Detect crashed agents, restart them, then run GitHub anomaly detection."""
        crashed: list[str] = []
        for agent in self.agents:
            if not agent.is_alive():
                exit_code = agent.process.returncode if agent.process else None
                logger.warning(f"{agent.name} is not running (exit_code={exit_code})")
                if agent.restart():
                    crashed.append(agent.name)
                else:
                    logger.error(
                        f"{agent.name} will not be restarted. Manual intervention required."
                    )

        try:
            snapshot = self._monitor.take_snapshot()
            anomalies = self._monitor.detect_anomalies(snapshot, agent_crashes=crashed)
            for anomaly in anomalies:
                try:
                    plan = self._intervention.decide(anomaly)
                    self._intervention.execute(plan)
                except Exception as e:
                    logger.error(f"Intervention failed for {anomaly}: {e}")
        except Exception as e:
            logger.error(f"Monitor error: {e}")

    def stop(self) -> None:
        """Stop all agents cleanly."""
        self._running = False
        logger.info("Orchestrator shutting down...")
        for agent in self.agents:
            agent.stop()
        logger.info("All agents stopped.")

    def _handle_signal(self, signum: int, frame: object) -> None:
        logger.info(f"Received signal {signum}, shutting down")
        self.stop()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Orchestrator - Claude-based supervisor for workflow agents"
    )
    parser.add_argument("repo", help="Repository in owner/repo format")
    parser.add_argument("--config", "-c", help="Path to config file")
    parser.add_argument(
        "--check-interval",
        type=int,
        default=60,
        help="Seconds between agent health checks (default: 60)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging"
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Validate repo config exists
    get_agent_config(args.repo, args.config)

    orchestrator = Orchestrator(
        repo=args.repo,
        check_interval=args.check_interval,
    )
    orchestrator.start()


if __name__ == "__main__":
    main()
