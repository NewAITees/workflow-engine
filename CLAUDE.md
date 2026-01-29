# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AI Workflow Engine: A 3-agent autonomous development system using GitHub as a message queue. Agents coordinate via GitHub labels and distributed locks to implement features from user stories.

**3-Agent Architecture:**
- **Planner Agent**: Converts user stories → technical specifications (GitHub Issues)
- **Worker Agent**: Picks up `status:ready` issues → implements → creates PR
- **Reviewer Agent**: Reviews PRs → approves/requests changes

## Development Commands

```bash
# Setup environment (after cloning)
uv sync --all-extras

# Run tests
uv run pytest

# Run tests with coverage
uv run pytest --cov=shared --cov=planner-agent --cov=worker-agent --cov=reviewer-agent

# Lint
uv run ruff check .

# Format
uv run ruff format .

# Type check
uv run mypy .

# Run specific test file
uv run pytest tests/test_lock.py -v
```

## Agent Commands

```bash
# Planner: Interactive mode (creates specs from stories)
uv run planner-agent/main.py owner/repo

# Planner: Non-interactive mode
uv run planner-agent/main.py owner/repo --story "Add user search feature"

# Worker: Daemon mode (polls for status:ready issues)
uv run worker-agent/main.py owner/repo

# Worker: Process one issue only (testing)
uv run worker-agent/main.py owner/repo --once --verbose

# Reviewer: Daemon mode (polls for status:reviewing PRs)
uv run reviewer-agent/main.py owner/repo

# Reviewer: Process one PR only (testing)
uv run reviewer-agent/main.py owner/repo --once --verbose
```

## Architecture & Design Patterns

### GitHub as Message Queue

This system uses GitHub Issues and PRs as a message queue, coordinated by labels instead of a traditional queue system. Benefits:
- No external infrastructure needed
- Built-in UI for monitoring
- Audit trail via comments and label transitions

### Label-Driven State Machine

Workflow states are encoded as labels:
- `status:ready` → Worker picks up
- `status:implementing` → Worker is working
- `status:reviewing` → Reviewer picks up
- `status:in-review` → Reviewer is working
- `status:approved` / `status:changes-requested` → Final states
- `status:failed` → Error state

State transitions are atomic and protected by the distributed lock mechanism.

### Distributed Lock Mechanism

Multi-agent coordination without a lock server, using GitHub comments + labels:

1. **ACK Comment**: Agent posts timestamped comment `ACK:worker:agent-id-123:1234567890`
2. **Wait**: 2-second grace period for race condition detection
3. **Conflict Detection**: Check if our ACK is the earliest within last 30 seconds
4. **Label Transition**: Winner transitions label (e.g., `status:ready` → `status:implementing`)
5. **Verification**: Confirm label was successfully applied

**Critical implementation details:**
- Only ACKs within 30-second window are considered valid (prevents stale locks)
- Earliest timestamp wins
- If label transition fails, lock acquisition fails
- See `shared/lock.py:LockManager` for implementation

### LLM Backend Abstraction

The system supports multiple LLM backends via `shared/llm_client.py:LLMClient`:

**Backends:**
- `codex`: OpenAI Codex CLI (`codex exec` command)
- `claude`: Claude Code CLI (`claude` command)

**Configuration** (`config/repos.yml`):
```yaml
repositories:
  - name: owner/repo
    llm_backend: codex  # or "claude"
    codex_cli: codex    # Path to codex CLI
    claude_cli: claude  # Path to claude CLI
```

**Key methods:**
- `generate_implementation()`: Takes spec + repo context → generates implementation
- `review_code()`: Takes spec + diff → generates review
- `create_spec()`: Takes user story → generates technical spec

The LLM client builds appropriate CLI commands for each backend, handling differences in:
- Command structure (`codex exec` vs `claude -p`)
- Tool restriction syntax
- Timeout handling

### Git Operations Isolation

Each agent clones repos into isolated workspaces to prevent conflicts:
- Default: `~/.workflow-engine/workspaces/<repo-name>/`
- Each issue gets a branch: `auto/issue-<number>`
- Worker creates PR after implementation
- Workspaces persist across runs for efficiency

See `worker-agent/git_operations.py:GitOperations` for clone/branch/commit logic.

## Configuration

**Required setup:**
1. Copy `config/repos.yml.example` → `config/repos.yml`
2. Configure repositories and LLM backend
3. Ensure CLI tools are installed: `gh`, `codex` or `claude`, `git`
4. Authenticate: `gh auth login`

**Config structure** (`config/repos.yml`):
```yaml
repositories:
  - name: owner/repo
    poll_interval: 30           # Seconds between checks
    work_dir: ~/.workflow-engine/workspaces  # Clone location
    llm_backend: codex         # "codex" or "claude"
    codex_cli: codex           # Codex CLI path
    claude_cli: claude         # Claude CLI path
    gh_cli: gh                 # GitHub CLI path
```

## Testing Strategy

Tests use pytest with async support:
- `tests/test_lock.py`: Distributed lock mechanism
- `tests/test_github_client.py`: GitHub API wrapper
- `tests/test_llm_client.py`: LLM backend abstraction
- `tests/test_git_operations.py`: Git workflow operations

Mock `gh` CLI responses where possible to avoid hitting GitHub API during tests.

## Common Patterns

**Adding a new agent:**
1. Create `<agent>-agent/main.py` with agent class
2. Initialize `GitHubClient`, `LockManager`, `LLMClient`
3. Implement polling loop with `try_lock_issue()` or `try_lock_pr()`
4. Handle failures with `lock.mark_failed()`

**Adding a new status:**
1. Add label to GitHub repository
2. Update state machine logic in relevant agents
3. Add constants to agent classes (e.g., `STATUS_NEW_STATE`)

**Debugging lock conflicts:**
- Use `--verbose` flag to see ACK comment timestamps
- Check GitHub issue/PR comments for ACK messages
- Verify system clocks are synchronized if running distributed
