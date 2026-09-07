#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib.sh"

query_file="${QUERY_FILE:-topics-qrels/queries.tsv}"
if [[ -s "${query_file}" ]]; then
  echo "Query file already exists: ${query_file}"
  exit 0
fi

mkdir -p "$(dirname "${query_file}")" data
run_python scripts_build_index/decrypt_dataset.py \
  --output data/browsecomp_plus_decrypted.jsonl \
  --generate-tsv "${query_file}"
echo "Generated ${query_file}"
