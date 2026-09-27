#!/usr/bin/env bash
# One-time setup of the Atom Neutron dense retrieval service on a Linux GPU box.
# Usage: HF_TOKEN=<token with access to CrowtherLabs> bash neutron-retrieval/setup.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${NEUTRON_VENV:-/workspace/neutron-venv}"
INDEX_ROOT="${NEUTRON_INDEX_ROOT:-/workspace/neutron-index}"
export HF_HOME="${HF_HOME:-/workspace/huggingface}"

if [[ -z "${HF_TOKEN:-}" ]]; then
  echo "HF_TOKEN is required: the adapter CrowtherLabs/Atom-Neutron-emb-0.6b is private." >&2
  exit 1
fi

if [[ ! -x "${VENV}/bin/python" ]]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv "${VENV}" --python 3.12
  else
    python3 -m venv "${VENV}"
  fi
fi
PY="${VENV}/bin/python"
pip_install() {
  if command -v uv >/dev/null 2>&1; then
    VIRTUAL_ENV="${VENV}" uv pip install "$@"
  else
    "${PY}" -m pip install "$@"
  fi
}

# cu128 wheels cover Ampere through Blackwell; older cu12x builds lack sm_120 kernels
# and only fail on the first real operation.
pip_install --index-url https://download.pytorch.org/whl/cu128 torch
pip_install -r "${HERE}/requirements.txt"

"${PY}" - <<'PY'
import torch
assert torch.cuda.is_available(), "CUDA is not available"
major, minor = torch.cuda.get_device_capability(0)
arch = f"sm_{major}{minor}"
assert arch in torch.cuda.get_arch_list(), f"torch {torch.__version__} has no {arch} kernels"
a = torch.randn(1024, 1024, device="cuda", dtype=torch.bfloat16)
print("GPU OK:", torch.cuda.get_device_name(0), arch, torch.__version__, float((a @ a).float().abs().sum()) > 0)
PY

INDEX_ROOT="${INDEX_ROOT}" "${PY}" - <<'PY'
import os
from huggingface_hub import snapshot_download
token = os.environ["HF_TOKEN"]
print(snapshot_download("Qwen/Qwen3-Embedding-0.6B"))
print(snapshot_download("CrowtherLabs/Atom-Neutron-emb-0.6b", token=token, allow_patterns=["adapter_*", "*.md"]))
print(snapshot_download("Tevatron/browsecomp-plus-indexes", repo_type="dataset",
                        allow_patterns=["qwen3-embedding-0.6b/*"], local_dir=os.environ["INDEX_ROOT"]))
print(snapshot_download("Tevatron/browsecomp-plus-corpus", repo_type="dataset"))
PY

echo "Setup complete. Index: ${INDEX_ROOT}/qwen3-embedding-0.6b"
echo "Start the service with: bash ${HERE}/run_server.sh"
