#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib.sh"

require_command nvidia-smi
require_command vllm
require_command curl
require_env BASE_MODEL
require_configured_env MODEL_NAME
require_env VLLM_PORT

# With ADAPTER_REPO set, vLLM serves BASE_MODEL under its own name plus the
# LoRA adapter under ADAPTER_NAME. The benchmark must request the adapter name:
# requesting the base name silently runs the model without the adapter.
adapter_repo="${ADAPTER_REPO:-}"
adapter_name="${ADAPTER_NAME:-}"
if [[ -n "${adapter_repo}" ]]; then
  [[ -n "${adapter_name}" ]] || adapter_name="$(basename "${adapter_repo}")"
  if [[ "${MODEL_NAME}" != "${adapter_name}" ]]; then
    echo "MODEL_NAME (${MODEL_NAME}) must equal the adapter name (${adapter_name})." >&2
    echo "Otherwise every benchmark request runs ${BASE_MODEL} without the adapter." >&2
    exit 1
  fi
  echo "Starting ${BASE_MODEL} with LoRA adapter ${adapter_repo} as ${adapter_name} on ${VLLM_HOST:-0.0.0.0}:${VLLM_PORT}"
else
  echo "Starting standalone model ${BASE_MODEL} as ${MODEL_NAME} on ${VLLM_HOST:-0.0.0.0}:${VLLM_PORT}"
fi

if curl -fsS "http://127.0.0.1:${VLLM_PORT}/v1/models" >/dev/null 2>&1; then
  echo "A model server already answers on port ${VLLM_PORT}; refusing to start a second one." >&2
  exit 1
fi

mkdir -p "$(dirname "${VLLM_LOG_FILE:-runs/vllm.log}")"
log_file="${VLLM_LOG_FILE:-runs/vllm.log}"
pid_file="${VLLM_PID_FILE:-runs/vllm.pid}"

vllm_args=(
  serve "${BASE_MODEL}"
  --host "${VLLM_HOST:-0.0.0.0}"
  --port "${VLLM_PORT}"
  --dtype "${VLLM_DTYPE:-bfloat16}"
  --max-model-len "${VLLM_MAX_MODEL_LEN:-131072}"
  --gpu-memory-utilization "${VLLM_GPU_MEMORY_UTILIZATION:-0.92}"
  --max-num-seqs "${VLLM_MAX_NUM_SEQS:-2}"
  --enable-auto-tool-choice
  --tool-call-parser "${VLLM_TOOL_CALL_PARSER:-qwen3_xml}"
  --reasoning-parser "${VLLM_REASONING_PARSER:-qwen3}"
)

if [[ -n "${adapter_repo}" ]]; then
  lora_module="$(printf '{"name":"%s","path":"%s","base_model_name":"%s"}'     "${adapter_name}" "${adapter_repo}" "${BASE_MODEL}")"
  vllm_args+=(
    --enable-lora
    --max-lora-rank "${VLLM_MAX_LORA_RANK:-16}"
    --lora-modules "${lora_module}"
  )
else
  vllm_args+=(--served-model-name "${MODEL_NAME}")
fi

if bool_env "${VLLM_LANGUAGE_MODEL_ONLY:-1}"; then
  vllm_args+=(--language-model-only)
fi

echo "Launching vLLM; log=${log_file}, pid=${pid_file}"
nohup vllm "${vllm_args[@]}" >"${log_file}" 2>&1 &
vllm_pid=$!
echo "${vllm_pid}" >"${pid_file}"

for _ in $(seq 1 "${VLLM_READY_ATTEMPTS:-120}"); do
  if curl -fsS "http://127.0.0.1:${VLLM_PORT}/health" >/dev/null 2>&1; then
    echo "vLLM health endpoint is ready (pid ${vllm_pid})."
    models="$(curl -fsS "http://127.0.0.1:${VLLM_PORT}/v1/models")"
    echo "${models}"
    if [[ -n "${adapter_repo}" && "${models}" != *"\"${adapter_name}\""* ]]; then
      echo "vLLM is up but does not list adapter ${adapter_name}; inspect ${log_file}" >&2
      exit 1
    fi
    exit 0
  fi
  if ! kill -0 "${vllm_pid}" 2>/dev/null; then
    echo "vLLM exited before becoming ready; inspect ${log_file}" >&2
    tail -n 80 "${log_file}" >&2 || true
    exit 1
  fi
  sleep "${VLLM_READY_INTERVAL_SECONDS:-5}"
done

echo "Timed out waiting for vLLM; inspect ${log_file}" >&2
exit 1
