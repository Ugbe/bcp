#!/usr/bin/env bash
set -euo pipefail

REMOTE_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${REMOTE_SCRIPT_DIR}/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${REPO_ROOT}/.venv/bin/python}"

if [[ ! -d "${REPO_ROOT}/.git" ]]; then
  echo "Run this command from a git clone of BrowseComp-Plus." >&2
  exit 1
fi

cd "${REPO_ROOT}"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

require_file() {
  [[ -f "$1" ]] || {
    echo "Missing required file: $1" >&2
    exit 1
  }
}

require_env() {
  local name="$1"
  [[ -n "${!name:-}" ]] || {
    echo "Missing required environment variable ${name}. Copy .env.example to .env and fill it in." >&2
    exit 1
  }
}

require_configured_env() {
  local name="$1"
  require_env "${name}"
  if [[ "${!name}" == *REPLACE_* || "${!name}" == *"<"* || "${!name}" == *">"* ]]; then
    echo "Environment variable ${name} still contains a placeholder." >&2
    exit 1
  fi
}

bool_env() {
  case "${1:-0}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

run_python() {
  require_file "${PYTHON_BIN}"
  "${PYTHON_BIN}" "$@"
}

run_python_module() {
  require_file "${PYTHON_BIN}"
  "${PYTHON_BIN}" -m "$@"
}

run_name="${RUN_NAME:-atom-electron-1.3-9b-remote}"
run_dir="${REPO_ROOT}/runs/${run_name}"
eval_dir="${REPO_ROOT}/evals/${run_name}"
