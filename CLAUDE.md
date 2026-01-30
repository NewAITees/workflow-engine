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
- `status:implementing` → Worker: generating tests & implementation
- `status:testing` → Worker: running tests (TDD workflow)
- `status:reviewing` → Reviewer picks up (after CI passes)
- `status:in-review` → Reviewer is working
- `status:approved` / `status:changes-requested` → Final states
- `status:ci-failed` → CI checks failed after automatic fix attempts
- `status:failed` → Error state

State transitions are atomic and protected by the distributed lock mechanism.

**TDD Flow (Worker):**
```
status:ready → status:implementing → status:testing → status:reviewing
                     ↑                      ↓
                     └──────(retry)─────────┘
```

### Distributed Lock Mechanism

Multi-agent coordination without a lock server, using GitHub comments + agent IDs:

1. **Pre-Check**: Check for existing active locks (within 30-minute timeout)
2. **ACK Comment**: Agent posts timestamped comment `ACK:worker:agent-id-123:1234567890`
3. **Wait**: 2-second grace period for race condition detection
4. **Conflict Detection**: Check if our ACK is the earliest within last 30 seconds
5. **Label Transition**: Winner transitions label (e.g., `status:ready` → `status:implementing`)
6. **Verification**: Confirm label was successfully applied

**Critical implementation details:**
- **30-minute timeout**: Locks expire after 30 minutes, enabling crash recovery
- **Agent ID tracking**: Each agent has unique ID (e.g., `worker-a1b2c3d4`)
- **30-second conflict window**: Only ACKs within last 30 seconds compete
- **Earliest timestamp wins**: Among recent ACKs, earliest gets the lock
- **Automatic resumption**: After timeout, any agent can take over
- If label transition fails, lock acquisition fails
- See `shared/lock.py:LockManager` for implementation

**Crash Recovery:**
- If Worker crashes, lock expires after 30 minutes
- Another Worker can automatically resume the work
- Original Worker can continue if lock still active

### TDD (Test-Driven Development) Workflow

Worker Agent follows strict TDD principles:

**1. Test Generation First**
- LLM generates pytest tests based on specification
- Tests are committed before implementation
- Tests define expected behavior

**2. Implementation**
- LLM generates implementation to pass tests
- Implementation is committed

**3. Automatic Test Execution**
- Worker runs pytest on generated tests
- Captures stdout/stderr for feedback

**4. Retry on Failure (max 3 attempts)**
- If tests fail, Worker provides failure output to LLM
- LLM regenerates implementation addressing test failures
- Process repeats until tests pass or max retries reached

**5. PR Creation**
- PR is created only after all tests pass
- PR description includes TDD metrics (attempts, test results)

**Benefits:**
- ✅ Ensures code quality through automated testing
- ✅ Reduces review cycles by catching bugs early
- ✅ Provides detailed feedback loop for LLM
- ✅ Transparent progress tracking via GitHub comments

**Implementation:**
```python
# 1. Generate tests
test_result = llm.generate_tests(spec, repo_context, work_dir)

# 2. Generate implementation (with retry)
for attempt in range(MAX_RETRIES):
    impl_result = llm.generate_implementation(spec, repo_context, work_dir)

    # 3. Run tests
    test_passed, output = _run_tests(issue_number)

    if test_passed:
        break  # Success!

    # 4. Provide feedback for retry
    spec += f"\n\n## Test Failure\n{output}"
```

See `worker-agent/main.py:_try_process_issue()` for full implementation.

### CI Auto-Fix Workflow

After PR creation, Worker Agent automatically monitors CI status and attempts to fix failures:

**1. CI Monitoring**
- Worker waits for CI checks to complete (10-minute timeout)
- Polls GitHub CI status every 30 seconds
- Checks for success/failure/pending states

**2. Automatic Fix Loop (max 3 attempts)**
- If CI fails, Worker retrieves detailed failure logs
- LLM analyzes logs and generates fixes
- Fix is committed and pushed to PR branch
- Worker waits for CI to run again

**3. Failure Handling**
- If all retries fail, PR is marked with `status:ci-failed`
- Comments explain the failure and request manual intervention
- If CI times out (pending), PR proceeds to review with warning comment

**Benefits:**
- ✅ Reduces manual intervention for common CI failures
- ✅ Faster iteration cycles with automatic fixes
- ✅ Detailed CI logs provided to LLM for context
- ✅ Clear failure tracking via labels and comments

**Configuration:**
```python
MAX_CI_RETRIES = 3  # Number of automatic fix attempts
CI_WAIT_TIMEOUT = 600  # 10 minutes max wait for CI
CI_CHECK_INTERVAL = 30  # Poll CI every 30 seconds
```

**Flow:**
```
PR created → Wait for CI
                 ↓
            CI passed? → Yes → status:reviewing
                 ↓ No
            Get CI logs → Fix code → Push → Wait for CI
                 ↑                             ↓
                 └──────(retry max 3)──────────┘
                             ↓ Max retries
                        status:ci-failed
```

See `worker-agent/main.py:_wait_for_ci()` and `_get_ci_failure_logs()` for implementation.

### Agent ID Management

Each agent instance gets a unique identifier for tracking and coordination:

**ID Generation:**
```python
self.agent_id = f"worker-{uuid.uuid4().hex[:8]}"
# Example: worker-a1b2c3d4
```

**Usage:**
- Posted in ACK comments for lock tracking
- Included in all log messages
- Visible in GitHub issue/PR comments
- Enables multi-agent deployment

**Benefits:**
- Clear ownership: Know which agent is processing what
- Debugging: Trace actions back to specific agent instance
- Coordination: Multiple agents can run without conflicts
- Monitoring: Track agent behavior and performance

**Example ACK comment:**
```
ACK:worker:worker-a1b2c3d4:1706123456789
```

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
- `generate_tests()`: Takes spec + repo context → generates pytest tests (TDD)
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
