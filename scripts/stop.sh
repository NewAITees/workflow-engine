#!/usr/bin/env bash
#
# Stop all running workflow agents.
#
# Usage:
#   ./stop.sh [owner/repo]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_DIR="$(dirname "$SCRIPT_DIR")"
REPO="${1:-}"

echo "=========================================="
echo "  Workflow Engine - Stop Agents"
echo "=========================================="
echo ""

# Stop tmux session if exists
if [ -n "$REPO" ]; then
    SESSION_NAME="workflow-${REPO//\//-}"
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        echo "Stopping tmux session: $SESSION_NAME"
        tmux kill-session -t "$SESSION_NAME"
    fi
fi

# Stop screen session if exists
if [ -n "$REPO" ]; then
    SESSION_NAME="workflow-${REPO//\//-}"
    screen -S "$SESSION_NAME" -X quit 2>/dev/null && echo "Stopped screen session: $SESSION_NAME" || true
fi

# Kill processes from PID files
LOG_DIR="$ENGINE_DIR/logs"
if [ -f "$LOG_DIR/worker.pid" ]; then
    PID=$(cat "$LOG_DIR/worker.pid")
    if kill -0 "$PID" 2>/dev/null; then
        echo "Stopping Worker Agent (PID: $PID)"
        kill "$PID" 2>/dev/null || true
    fi
    rm -f "$LOG_DIR/worker.pid"
fi

if [ -f "$LOG_DIR/reviewer.pid" ]; then
    PID=$(cat "$LOG_DIR/reviewer.pid")
    if kill -0 "$PID" 2>/dev/null; then
        echo "Stopping Reviewer Agent (PID: $PID)"
        kill "$PID" 2>/dev/null || true
    fi
    rm -f "$LOG_DIR/reviewer.pid"
fi

# Kill any remaining Python processes running the agents
echo ""
echo "Checking for remaining agent processes..."
pkill -f "planner-agent/main.py" 2>/dev/null && echo "Stopped Planner Agent" || true
pkill -f "worker-agent/main.py" 2>/dev/null && echo "Stopped Worker Agent" || true
pkill -f "reviewer-agent/main.py" 2>/dev/null && echo "Stopped Reviewer Agent" || true

echo ""
echo "Done."
