# Todo Demo

## How to run
1. Setup repository metadata:
   - `scripts/setup-repository.sh --repo owner/todo-demo --dry-run`
2. Create a TODO feature issue with `status:ready`.
3. Run one worker cycle:
   - `uv run worker-agent/main.py owner/todo-demo --once`
4. Run one reviewer cycle:
   - `uv run reviewer-agent/main.py owner/todo-demo --once`

## Expected result
- Logs include repository start/success/failure with duration.
- Worker handles `status:ready` issue and creates/updates PR.
- Reviewer processes `status:reviewing` PR.

## Expected log snippet
- `[repo=owner/todo-demo] started`
- `[repo=owner/todo-demo] success duration=...s`
