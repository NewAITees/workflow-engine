# Troubleshooting

## Authentication (`gh auth status` fails)
- Symptom: setup script exits with auth error.
- Fix:
  - `gh auth login`
  - `gh auth status`

## Permission denied on labels/templates
- Symptom: API returns 403 or push rejected.
- Fix:
  - Verify repo write/admin permission.
  - Ensure token scopes include repository write access.

## Rate limit
- Symptom: API returns rate-limit errors.
- Fix:
  - `gh api rate_limit`
  - Wait for reset window, then re-run setup.

## Partial failures in multi-repo run
- Symptom: some repos succeed, others fail.
- Fix:
  - Run with `FAIL_FAST=false` to collect full failure list.
  - Re-run failed repos only via `TARGET_REPOS=org/a,org/b ... --once`.
