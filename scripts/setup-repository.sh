#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
LABELS_FILE="${ROOT_DIR}/config/labels.yml"
TEMPLATES_DIR="${ROOT_DIR}/templates/github"
AGENTS_TEMPLATE="${ROOT_DIR}/templates/AGENTS.md"

REPO=""
DRY_RUN=false

usage() {
  cat <<USAGE
Usage: scripts/setup-repository.sh --repo owner/repo [--dry-run]

Options:
  --repo <owner/repo>  Target repository
  --dry-run            Print planned changes without mutating GitHub or pushing commits
USAGE
}

log() {
  printf '%s\n' "$*"
}

run_mutating() {
  if [[ "${DRY_RUN}" == "true" ]]; then
    log "[DRY-RUN] $*"
    return 0
  fi
  "$@"
}

url_encode() {
  python3 - "$1" <<'PY'
import sys
from urllib.parse import quote
print(quote(sys.argv[1], safe=""))
PY
}

parse_labels() {
  local profile="$1"
  python3 - "${LABELS_FILE}" "${profile}" <<'PY'
import sys
from pathlib import Path
import yaml

path = Path(sys.argv[1])
profile = sys.argv[2]
data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
profiles = data.get("profiles", {})
labels = profiles.get(profile, [])
for item in labels:
    name = str(item.get("name", "")).strip()
    if not name:
        continue
    color = str(item.get("color", "0E8A16")).strip()
    description = str(item.get("description", "")).strip()
    print(f"{name}\t{color}\t{description}")
PY
}

sync_file() {
  local src="$1"
  local dst="$2"

  mkdir -p "$(dirname "${dst}")"

  if [[ ! -f "${dst}" ]]; then
    if [[ "${DRY_RUN}" == "true" ]]; then
      log "[DRY-RUN] create ${dst}"
    else
      cp "${src}" "${dst}"
      log "created ${dst}"
    fi
    return
  fi

  if cmp -s "${src}" "${dst}"; then
    log "skip ${dst} (no changes)"
    return
  fi

  if [[ "${DRY_RUN}" == "true" ]]; then
    log "[DRY-RUN] update ${dst}"
  else
    cp "${src}" "${dst}"
    log "updated ${dst}"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      if [[ $# -lt 2 ]]; then
        echo "--repo requires owner/repo" >&2
        usage
        exit 1
      fi
      REPO="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -z "${REPO}" ]]; then
  echo "--repo is required" >&2
  usage
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "GitHub authentication is required." >&2
  echo "Run: gh auth login" >&2
  echo "Then re-run: scripts/setup-repository.sh --repo ${REPO}${DRY_RUN:+ --dry-run}" >&2
  exit 1
fi

if [[ ! -f "${LABELS_FILE}" ]]; then
  echo "Missing labels file: ${LABELS_FILE}" >&2
  exit 1
fi

if [[ "${DRY_RUN}" == "true" ]]; then
  log "Running in dry-run mode. No remote changes will be applied."
fi

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT
REPO_DIR="${WORK_DIR}/repo"

log "Cloning ${REPO} ..."
gh repo clone "${REPO}" "${REPO_DIR}" >/dev/null

PROFILE="default"
while IFS=$'\t' read -r label_name label_color label_description; do
  [[ -z "${label_name}" ]] && continue
  encoded_name="$(url_encode "${label_name}")"
  if gh api "repos/${REPO}/labels/${encoded_name}" >/dev/null 2>&1; then
    if [[ "${DRY_RUN}" == "true" ]]; then
      log "[DRY-RUN] update label ${label_name}"
    else
      run_mutating gh api \
        --method PATCH \
        -H "Accept: application/vnd.github+json" \
        "repos/${REPO}/labels/${encoded_name}" \
        -f "new_name=${label_name}" \
        -f "color=${label_color}" \
        -f "description=${label_description}" >/dev/null
      log "updated label ${label_name}"
    fi
  else
    if [[ "${DRY_RUN}" == "true" ]]; then
      log "[DRY-RUN] create label ${label_name}"
    else
      run_mutating gh api \
        --method POST \
        -H "Accept: application/vnd.github+json" \
        "repos/${REPO}/labels" \
        -f "name=${label_name}" \
        -f "color=${label_color}" \
        -f "description=${label_description}" >/dev/null
      log "created label ${label_name}"
    fi
  fi
done < <(parse_labels "${PROFILE}")

sync_file "${TEMPLATES_DIR}/PULL_REQUEST_TEMPLATE.md" \
  "${REPO_DIR}/.github/PULL_REQUEST_TEMPLATE.md"

for issue_tpl in "${TEMPLATES_DIR}/ISSUE_TEMPLATE"/*; do
  [[ -e "${issue_tpl}" ]] || continue
  sync_file "${issue_tpl}" \
    "${REPO_DIR}/.github/ISSUE_TEMPLATE/$(basename "${issue_tpl}")"
done

sync_file "${AGENTS_TEMPLATE}" "${REPO_DIR}/AGENTS.md"

if [[ "${DRY_RUN}" == "true" ]]; then
  log "[DRY-RUN] setup completed for ${REPO}"
  exit 0
fi

pushd "${REPO_DIR}" >/dev/null
if [[ -n "$(git status --porcelain)" ]]; then
  git add .github AGENTS.md
  git commit -m "chore: bootstrap workflow-engine templates and labels" >/dev/null
  git push >/dev/null
  log "pushed template updates to ${REPO}"
else
  log "no template changes to commit"
fi
popd >/dev/null

log "setup completed for ${REPO}"
