#!/usr/bin/env bash
#
# Launch all three workflow agents in parallel using tmux.
#
# Usage:
#   ./launch.sh <owner/repo> [mode] [config]
#
# Modes:
#   tmux     - Launch in tmux session with split panes (default)
#   screen   - Launch in screen session
#   bg       - Launch as background processes
#
# Examples:
#   ./launch.sh owner/repo
#   ./launch.sh owner/repo tmux
#   ./launch.sh owner/repo bg /path/to/config.yml

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_DIR="$(dirname "$SCRIPT_DIR")"

# Arguments
REPO="${1:-}"
MODE="${2:-tmux}"
CONFIG="${3:-}"

# Validation
if [ -z "$REPO" ]; then
    echo "Usage: $0 <owner/repo> [mode] [config]"
    echo ""
    echo "Modes:"
    echo "  tmux   - Launch in tmux session with split panes (default)"
    echo "  screen - Launch in screen session"
    echo "  bg     - Launch as background processes"
    exit 1
fi

# Build config argument
CONFIG_ARG=""
if [ -n "$CONFIG" ]; then
    CONFIG_ARG="--config $CONFIG"
fi

# Agent commands
PLANNER_CMD="uv run $ENGINE_DIR/planner-agent/main.py $REPO $CONFIG_ARG"
WORKER_CMD="uv run $ENGINE_DIR/worker-agent/main.py $REPO $CONFIG_ARG"
REVIEWER_CMD="uv run $ENGINE_DIR/reviewer-agent/main.py $REPO $CONFIG_ARG"

SESSION_NAME="workflow-${REPO//\//-}"

launch_tmux() {
    echo "=========================================="
    echo "  Workflow Engine Launcher (tmux)"
    echo "=========================================="
    echo ""
    echo "Repository: $REPO"
    echo "Session: $SESSION_NAME"
    echo ""

    # Check if tmux is available
    if ! command -v tmux &> /dev/null; then
        echo "Error: tmux not found. Install it or use 'bg' mode."
        exit 1
    fi

    # Kill existing session if exists
    tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true

    # Create new session with Worker agent
    tmux new-session -d -s "$SESSION_NAME" -n "agents" -c "$ENGINE_DIR"
    tmux send-keys -t "$SESSION_NAME" "echo '=== Worker Agent ===' && $WORKER_CMD" C-m

    # Split horizontally for Reviewer
    tmux split-window -h -t "$SESSION_NAME" -c "$ENGINE_DIR"
    tmux send-keys -t "$SESSION_NAME" "echo '=== Reviewer Agent ===' && $REVIEWER_CMD" C-m

    # Split the first pane vertically for Planner
    tmux select-pane -t "$SESSION_NAME:0.0"
    tmux split-window -v -t "$SESSION_NAME" -c "$ENGINE_DIR"
    tmux send-keys -t "$SESSION_NAME" "echo '=== Planner Agent ===' && $PLANNER_CMD" C-m

    # Set layout
    tmux select-layout -t "$SESSION_NAME" main-vertical

    # Attach to session
    echo "Attaching to tmux session..."
    echo "Use 'Ctrl+B D' to detach, 'tmux attach -t $SESSION_NAME' to reattach"
    echo ""
    tmux attach-session -t "$SESSION_NAME"
}

launch_screen() {
    echo "=========================================="
    echo "  Workflow Engine Launcher (screen)"
    echo "=========================================="
    echo ""
    echo "Repository: $REPO"
    echo "Session: $SESSION_NAME"
    echo ""

    # Check if screen is available
    if ! command -v screen &> /dev/null; then
        echo "Error: screen not found. Install it or use 'bg' mode."
        exit 1
    fi

    # Kill existing session if exists
    screen -S "$SESSION_NAME" -X quit 2>/dev/null || true

    # Create screenrc for multi-window setup
    SCREENRC=$(mktemp)
    cat > "$SCREENRC" << EOF
# Workflow Engine Screen Configuration
hardstatus alwayslastline
hardstatus string '%{= kG}[ %{G}%H %{g}][%= %{=kw}%?%-Lw%?%{r}(%{W}%n*%f%t%?(%u)%?%{r})%{w}%?%+Lw%?%?%= %{g}][%{B}%Y-%m-%d %{W}%c %{g}]'

screen -t "Worker" bash -c "cd $ENGINE_DIR && $WORKER_CMD; exec bash"
screen -t "Reviewer" bash -c "cd $ENGINE_DIR && $REVIEWER_CMD; exec bash"
screen -t "Planner" bash -c "cd $ENGINE_DIR && $PLANNER_CMD; exec bash"

select 2
EOF

    echo "Starting screen session..."
    echo "Use 'Ctrl+A D' to detach, 'screen -r $SESSION_NAME' to reattach"
    echo "Use 'Ctrl+A N' to switch windows"
    echo ""
    screen -S "$SESSION_NAME" -c "$SCREENRC"
    rm -f "$SCREENRC"
}

launch_background() {
    echo "=========================================="
    echo "  Workflow Engine Launcher (background)"
    echo "=========================================="
    echo ""
    echo "Repository: $REPO"
    echo ""

    LOG_DIR="$ENGINE_DIR/logs"
    mkdir -p "$LOG_DIR"

    echo "Starting Worker Agent..."
    nohup bash -c "cd $ENGINE_DIR && $WORKER_CMD" > "$LOG_DIR/worker.log" 2>&1 &
    WORKER_PID=$!
    echo "  PID: $WORKER_PID, Log: $LOG_DIR/worker.log"

    echo "Starting Reviewer Agent..."
    nohup bash -c "cd $ENGINE_DIR && $REVIEWER_CMD" > "$LOG_DIR/reviewer.log" 2>&1 &
    REVIEWER_PID=$!
    echo "  PID: $REVIEWER_PID, Log: $LOG_DIR/reviewer.log"

    echo ""
    echo "Background agents started."
    echo ""
    echo "To view logs:"
    echo "  tail -f $LOG_DIR/worker.log"
    echo "  tail -f $LOG_DIR/reviewer.log"
    echo ""
    echo "To stop agents:"
    echo "  kill $WORKER_PID $REVIEWER_PID"
    echo ""

    # Save PIDs for later
    echo "$WORKER_PID" > "$LOG_DIR/worker.pid"
    echo "$REVIEWER_PID" > "$LOG_DIR/reviewer.pid"

    echo "Starting Planner Agent (interactive)..."
    echo ""
    cd "$ENGINE_DIR" && $PLANNER_CMD

    # Cleanup when planner exits
    echo ""
    echo "Stopping background agents..."
    kill "$WORKER_PID" "$REVIEWER_PID" 2>/dev/null || true
}

# Main
case "$MODE" in
    tmux)
        launch_tmux
        ;;
    screen)
        launch_screen
        ;;
    bg|background)
        launch_background
        ;;
    *)
        echo "Unknown mode: $MODE"
        echo "Valid modes: tmux, screen, bg"
        exit 1
        ;;
esac
