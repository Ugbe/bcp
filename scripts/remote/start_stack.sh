#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib.sh"

require_command tmux
require_file "${PYTHON_BIN}"
require_file "${QUERY_FILE:-topics-qrels/queries.tsv}"
require_configured_env MODEL_BASE_URL
require_configured_env MODEL_NAME
require_configured_env BCP_RETRIEVAL_URL
require_configured_env BCP_TOKEN
require_configured_env AZURE_OPENAI_API_KEY
require_configured_env AZURE_OPENAI_ENDPOINT
require_configured_env AZURE_OPENAI_DEPLOYMENT
require_configured_env AZURE_OPENAI_API_VERSION

mkdir -p "${run_dir}" "${eval_dir}" runs
session="${TMUX_SESSION:-bcp-${run_name}}"

if tmux has-session -t "${session}" 2>/dev/null; then
  echo "tmux session already exists: ${session}"
  echo "Attach with: tmux attach -t ${session}"
  exit 0
fi

tmux new-session -d -s "${session}" -n dashboard \
  "cd '${REPO_ROOT}' && '${PYTHON_BIN}' -m uvicorn dashboard.app:app --host 0.0.0.0 --port '${DASHBOARD_PORT:-7860}'"
tmux new-window -t "${session}" -n evaluator \
  "cd '${REPO_ROOT}' && bash '${SCRIPT_DIR}/run_evaluator.sh'"
tmux new-window -t "${session}" -n benchmark \
  "cd '${REPO_ROOT}' && bash '${SCRIPT_DIR}/run_benchmark.sh'"

echo "Started ${session} with dashboard, evaluator, and benchmark windows."
echo "Attach: tmux attach -t ${session}"
echo "Dashboard: http://127.0.0.1:${DASHBOARD_PORT:-7860}/"
