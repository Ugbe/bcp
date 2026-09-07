#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib.sh"

require_configured_env AZURE_OPENAI_API_KEY
require_configured_env AZURE_OPENAI_ENDPOINT
require_configured_env AZURE_OPENAI_DEPLOYMENT
require_configured_env AZURE_OPENAI_API_VERSION
require_file "${QUERY_FILE:-topics-qrels/queries.tsv}"
mkdir -p "${run_dir}" "${eval_dir}"

run_python scripts_evaluation/evaluate_with_azure.py \
  --input_dir "${run_dir}" \
  --eval_dir "${eval_dir}" \
  --query_file "${QUERY_FILE:-topics-qrels/queries.tsv}" \
  --watch \
  --exit_when_complete \
  --poll_interval "${EVAL_POLL_INTERVAL:-15}"
