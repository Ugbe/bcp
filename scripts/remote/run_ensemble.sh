#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib.sh"

require_env MODEL_BASE_URL
require_env MODEL_NAME
require_env BCP_RETRIEVAL_URL
require_env BCP_TOKEN
require_file "${QUERY_FILE:-topics-qrels/queries.tsv}"

ensemble_name="${ENSEMBLE_RUN_NAME:-${run_name}-ensemble}"
ensemble_dir="${REPO_ROOT}/runs/${ensemble_name}"
mkdir -p "${ensemble_dir}"

echo "Starting ${ENSEMBLE_ROLLOUTS:-4}-rollout ensemble in ${ensemble_dir}."
echo "Each qid is written once; diagnostics contain every rollout and pooled vote."

run_python search_agent/research_ensemble.py \
  --query "${QUERY_FILE:-topics-qrels/queries.tsv}" \
  --model "${MODEL_NAME}" \
  --model-url "${MODEL_BASE_URL}" \
  --model-api-key "${MODEL_API_KEY:-EMPTY}" \
  --output-dir "${ensemble_dir}" \
  --rollouts "${ENSEMBLE_ROLLOUTS:-4}" \
  --temperatures "${ENSEMBLE_TEMPERATURES:-0.2,0.6,0.6,0.8}" \
  --seed-base "${ENSEMBLE_SEED_BASE:-4100}" \
  --parallel-rollouts "${ENSEMBLE_PARALLEL_ROLLOUTS:-4}" \
  --num-threads "${NUM_THREADS:-1}" \
  --max-tokens "${MAX_TOKENS:-12000}" \
  --max-iterations "${MAX_ITERATIONS:-64}" \
  --max-tool-calls "${MAX_TOOL_CALLS:-32}" \
  --top-p "${TOP_P:-0.95}" \
  --top-k "${TOP_K:-20}" \
  --repetition-penalty "${REPETITION_PENALTY:-1.05}" \
  --k "${SEARCH_K:-10}" \
  --snippet-max-tokens "${SNIPPET_MAX_TOKENS:-512}" \
  --document-max-tokens "${DOCUMENT_MAX_TOKENS:-4096}" \
  --query-template "${QUERY_TEMPLATE:-QUERY_TEMPLATE_RESEARCH_LEDGER}" \
  --retrieval-novelty "${RETRIEVAL_NOVELTY:-server}" \
  --retrieval-seen-anchor-count "${RETRIEVAL_SEEN_ANCHOR_COUNT:-2}" \
  --pool-max-chars "${ENSEMBLE_POOL_MAX_CHARS:-60000}" \
  --verbose
