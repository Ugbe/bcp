#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib.sh"

require_configured_env MODEL_BASE_URL
require_configured_env MODEL_NAME
require_configured_env BCP_RETRIEVAL_URL
require_configured_env BCP_TOKEN
require_configured_env AZURE_OPENAI_API_KEY
require_configured_env AZURE_OPENAI_ENDPOINT
require_configured_env AZURE_OPENAI_DEPLOYMENT
require_configured_env AZURE_OPENAI_API_VERSION
run_python scripts_evaluation/smoke_test.py
