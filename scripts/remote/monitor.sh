#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib.sh"

# The monitor uses only the standard library, so fall back to the system
# interpreter when bootstrap.sh has not created the runner venv yet.
python_bin="${PYTHON_BIN}"
if [[ ! -x "${python_bin}" ]]; then
  require_command python3
  python_bin="$(command -v python3)"
fi

exec "${python_bin}" scripts_evaluation/monitor_progress.py \
  --run-name "${run_name}" \
  --query-file "${QUERY_FILE:-topics-qrels/queries.tsv}" \
  --interval "${MONITOR_INTERVAL:-30}" \
  "$@"
