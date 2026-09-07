#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib.sh"

require_command python3

if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env 2>/dev/null || true
  echo "Created .env from .env.example. Fill in model, retrieval, and Azure values before the smoke test."
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Creating lightweight runner environment at ${REPO_ROOT}/.venv"
  python3 -m venv "${REPO_ROOT}/.venv"
fi

"${PYTHON_BIN}" -m pip install --upgrade pip
"${PYTHON_BIN}" -m pip install --upgrade -r requirements-remote.txt

if ! command -v vllm >/dev/null 2>&1; then
  cat >&2 <<'EOF'
vLLM is not installed in this image. The harness can run against an existing
OpenAI-compatible endpoint, but the local model-serving launcher needs vLLM.
Use a vLLM-compatible Vast image, or explicitly install a tested vLLM build in
that image before running scripts/remote/serve_vllm.sh. Do not blindly replace
the image's torch/CUDA packages with the repo's full pyproject dependencies.
EOF
fi

mkdir -p data topics-qrels runs evals

if [[ ! -s "${QUERY_FILE:-topics-qrels/queries.tsv}" ]]; then
  echo "Preparing encrypted BrowseComp test queries..."
  run_python scripts_build_index/decrypt_dataset.py \
    --output data/browsecomp_plus_decrypted.jsonl \
    --generate-tsv "${QUERY_FILE:-topics-qrels/queries.tsv}"
else
  echo "Using existing query file: ${QUERY_FILE:-topics-qrels/queries.tsv}"
fi

echo "Bootstrap complete. Next:"
echo "  1. Fill in .env (secrets stay local and are gitignored)."
echo "  2. Run scripts/remote/serve_vllm.sh if this GPU serves the model."
echo "  3. Run scripts/remote/smoke_test.sh."
echo "  4. Run scripts/remote/start_stack.sh."
