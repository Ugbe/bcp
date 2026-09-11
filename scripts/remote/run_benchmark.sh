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

args=(
  search_agent/chat_client.py
  --query "${QUERY_FILE:-topics-qrels/queries.tsv}"
  --model "${MODEL_NAME}"
  --model-url "${MODEL_BASE_URL}"
  --model-api-key "${MODEL_API_KEY:-EMPTY}"
  --searcher-type remote
  --output-dir "${run_dir}"
  --num-threads "${NUM_THREADS:-2}"
  --max-tokens "${MAX_TOKENS:-4096}"
  --max-iterations "${MAX_ITERATIONS:-64}"
  --max-tool-calls "${MAX_TOOL_CALLS:-40}"
  --temperature "${TEMPERATURE:-0.6}"
  --top-p "${TOP_P:-0.95}"
  --top-k "${TOP_K:-20}"
  --repetition-penalty "${REPETITION_PENALTY:-1.05}"
  --k "${SEARCH_K:-10}"
  --snippet-max-tokens "${SNIPPET_MAX_TOKENS:-256}"
  --document-max-tokens "${DOCUMENT_MAX_TOKENS:-4096}"
  --get-document
  --query-template "${QUERY_TEMPLATE:-QUERY_TEMPLATE_RESEARCH_LEDGER}"
  --retrieval-novelty "${RETRIEVAL_NOVELTY:-server}"
  --retrieval-seen-anchor-count "${RETRIEVAL_SEEN_ANCHOR_COUNT:-0}"
  --verbose
)

if bool_env "${ENABLE_CONTEXT_COMPACTION:-1}"; then
  args+=(
    --context-compaction
    --context-window-tokens "${CONTEXT_WINDOW_TOKENS:-131072}"
    --context-compaction-trigger-tokens "${CONTEXT_COMPACTION_TRIGGER_TOKENS:-98304}"
    --context-compaction-keep-tool-rounds "${CONTEXT_COMPACTION_KEEP_TOOL_ROUNDS:-2}"
    --context-compaction-max-tokens "${CONTEXT_COMPACTION_MAX_TOKENS:-1536}"
    --context-compaction-reserve-tokens "${CONTEXT_COMPACTION_RESERVE_TOKENS:-8192}"
  )
fi

if bool_env "${ENABLE_EVIDENCE_NOTES:-0}"; then
  args+=(--evidence-notes --evidence-note-max-tokens "${EVIDENCE_NOTE_MAX_TOKENS:-700}")
fi

if bool_env "${ENABLE_FRESH_FINAL:-0}"; then
  args+=(
    --fresh-final-answer
    --fresh-final-max-tokens "${FRESH_FINAL_MAX_TOKENS:-1024}"
    --fresh-final-prompt-max-tokens "${FRESH_FINAL_PROMPT_MAX_TOKENS:-24000}"
  )
fi

if bool_env "${ENABLE_MULTI_QUERY_SEARCH:-0}"; then
  args+=(--multi-query-search)
fi

if bool_env "${ENABLE_DEEP_POOL_SEARCH:-0}"; then
  args+=(--deep-pool-search --deep-pool-k "${DEEP_POOL_K:-100}")
fi

if bool_env "${ENABLE_BULK_GET_DOCUMENTS:-0}"; then
  args+=(--bulk-get-documents --bulk-get-documents-max-docs "${BULK_GET_DOCUMENTS_MAX_DOCS:-10}")
fi

mkdir -p "${run_dir}"
echo "Starting benchmark in ${run_dir}; completed qids are skipped on resume."
run_python "${args[@]}"
