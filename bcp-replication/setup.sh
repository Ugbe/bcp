#!/bin/bash
# Rebuild the BrowseComp-Plus hybrid retrieval service on a fresh Vast.ai instance.
#
#   HF_TOKEN=hf_xxx ./setup.sh
#
# Idempotent: re-running skips completed downloads, the token cache and the
# decrypt step, and re-installs cleanly.
# Expect ~30-35 minutes: ~25 downloading the ~26 GB of weights and indexes, plus a
# few minutes building the rerank token cache.

set -euo pipefail

WORKSPACE="${WORKSPACE:-/workspace}"
REPO="${WORKSPACE}/BrowseComp-Plus"
BUNDLE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UPSTREAM_COMMIT="046949032b0328319cc9a02663a759ec601d9402"

# Ports: internal is what uvicorn binds, external is the Caddy auth edge.
# The external one must be an open port on the instance with in_use=false.
INTERNAL_PORT="${INTERNAL_PORT:-17070}"
EXTERNAL_PORT="${EXTERNAL_PORT:-10100}"

log() { echo -e "\n\033[1;36m==> $*\033[0m"; }

# The token ships in the bundle as hf_token.env; an explicit HF_TOKEN in the
# environment overrides it, which is how you rebuild with a rotated credential
# without editing the file.
if [[ -z "${HF_TOKEN:-}" && -s "${BUNDLE}/hf_token.env" ]]; then
    # shellcheck disable=SC1091
    set -a; . "${BUNDLE}/hf_token.env"; set +a
    echo "Using HF_TOKEN from ${BUNDLE}/hf_token.env"
fi
if [[ -z "${HF_TOKEN:-}" ]]; then
    echo "ERROR: HF_TOKEN is not set and hf_token.env is missing or empty."
    echo "       The fine-tuned adapters are in private repos and cannot be fetched"
    echo "       without it."
    echo "  HF_TOKEN=hf_xxx $0"
    exit 1
fi

# ---------------------------------------------------------------- 1. sanity
log "Checking hardware"
if ! command -v nvidia-smi >/dev/null; then
    echo "ERROR: no NVIDIA GPU visible."; exit 1
fi
VRAM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1)
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -1
# Measured peak on the original box (RTX 4090) is 23.6 GB of 24 GB during the
# stage-1 rerank batch -- weights are only ~17.5 GB, the rest is activations
# governed by --rerank-token-budget. 24 GB is the real floor, not 22.
if (( VRAM < 24000 )); then
    echo "WARNING: measured peak usage is ~23.6 GB; this GPU has ${VRAM} MiB."
    echo "         Lower --rerank-token-budget in /opt/supervisor-scripts/bcp-retrieval.sh"
    echo "         (32768 -> 16384 roughly halves the activation peak), or expect CUDA OOM."
fi

# ---------------------------------------------------------------- 2. repo
log "Fetching upstream repo @ ${UPSTREAM_COMMIT:0:10}"
if [[ ! -d "${REPO}/.git" ]]; then
    git clone https://github.com/texttron/BrowseComp-Plus.git "${REPO}"
fi
# --force so re-running is safe: we overwrite tracked files (search_r1_server.py,
# searchers/__init__.py) below, which would otherwise block the checkout.
git -C "${REPO}" checkout --force --quiet "${UPSTREAM_COMMIT}"

log "Applying our changes on top of upstream"
mkdir -p "${REPO}/notebooks"
cp "${BUNDLE}/searcher/searchers/hybrid_searcher.py" "${REPO}/searcher/searchers/"
cp "${BUNDLE}/searcher/searchers/__init__.py"        "${REPO}/searcher/searchers/"
cp "${BUNDLE}/searcher/search_r1_server.py"          "${REPO}/searcher/"
cp "${BUNDLE}/scripts_build_index/download_all.py"             "${REPO}/scripts_build_index/"
cp "${BUNDLE}/scripts_build_index/build_rerank_token_cache.py" "${REPO}/scripts_build_index/"
cp "${BUNDLE}/scripts_evaluation/eval_retrieval.py"            "${REPO}/scripts_evaluation/"
cp "${BUNDLE}/scripts_evaluation/tune_pipeline.py"             "${REPO}/scripts_evaluation/"
cp "${BUNDLE}/scripts_evaluation/ab_rerank.py"                 "${REPO}/scripts_evaluation/"
cp "${BUNDLE}/scripts_evaluation/bench_concurrency.py"         "${REPO}/scripts_evaluation/"
cp "${BUNDLE}/notebooks/test_retrieval.ipynb"                  "${REPO}/notebooks/"

# The searcher reads this itself, so every entrypoint (server, eval) gets the token.
printf 'HF_TOKEN=%s\n' "${HF_TOKEN}" > "${REPO}/.env"
chmod 600 "${REPO}/.env"

# ---------------------------------------------------------------- 3. deps
log "Installing OpenJDK 21 (Pyserini's Lucene backend needs a JVM)"
if ! command -v java >/dev/null; then
    apt-get update -qq && apt-get install -y -q openjdk-21-jdk-headless
fi
java -version 2>&1 | head -1

log "Installing Python dependencies"
# Deliberately NOT installing the repo's pyproject: it pulls vllm + deepspeed +
# tevatron, which would fight the preinstalled torch. The hybrid searcher uses
# transformers/peft/faiss directly and needs none of them.
source /venv/main/bin/activate
uv pip install -q -r "${BUNDLE}/requirements.txt"
python -c "import torch, faiss, transformers, peft; print('torch', torch.__version__, '| cuda', torch.cuda.is_available(), '| faiss gpus', faiss.get_num_gpus())"

# flash-attn is a *performance* dependency, not a correctness one: the searcher
# downgrades to sdpa with a log warning if it cannot import it (_resolve_attn).
# It is kept out of requirements.txt because there is no universal wheel -- pip
# would fall back to a ~1 hour source build. Try the prebuilt wheel matching this
# box's exact torch/CUDA/python, and move on if there isn't one.
if python -c "import flash_attn" 2>/dev/null; then
    echo "flash-attn already present: $(python -c 'import flash_attn; print(flash_attn.__version__)')"
else
    log "Trying a prebuilt flash-attn wheel (optional; sdpa fallback is fine)"
    FA_URL=$(python - <<'PYFA'
import torch, sys
t = torch.__version__.split("+")[0]
tm = ".".join(t.split(".")[:2])                       # e.g. 2.12
cu = "cu" + (torch.version.cuda or "0.0").replace(".", "")  # e.g. cu130
py = f"cp{sys.version_info.major}{sys.version_info.minor}"
print(
    "https://github.com/mjun0812/flash-attention-prebuild-wheels/releases/download/"
    f"v0.9.17/flash_attn-2.8.3%2B{cu}torch{tm}-{py}-{py}-linux_x86_64.whl"
)
PYFA
)
    if uv pip install -q "${FA_URL}" 2>/dev/null && python -c "import flash_attn" 2>/dev/null; then
        echo "flash-attn installed: ${FA_URL##*/}"
    else
        echo "NOTE: no matching flash-attn wheel (${FA_URL##*/})."
        echo "      The service will run on sdpa -- correct, just slower. To use flash"
        echo "      attention later, find a wheel for this torch/CUDA/python or build"
        echo "      from source, then: supervisorctl restart bcp-retrieval"
    fi
fi

# ---------------------------------------------------------------- 4. artifacts
log "Downloading models, indexes and corpus (~18 GB, the slow part)"
cd "${REPO}"
python scripts_build_index/download_all.py

# The service is launched with --rerank-token-cache. Without this directory the
# searcher logs one warning and then re-tokenizes ~200 documents on every request
# -- ~2.2s of avoidable single-threaded work per query. It degrades quietly, which
# is exactly why it is built here and not left as an optional follow-up step.
log "Building the rerank token cache (~1.4 GB, a few minutes on many cores)"
if [[ -s "${REPO}/indexes/rerank-token-cache/tokens.i32" ]]; then
    echo "already built, skipping"
else
    python scripts_build_index/build_rerank_token_cache.py
fi

log "Decrypting benchmark queries (only needed for evaluation)"
if [[ ! -s "${REPO}/data/queries.tsv" ]]; then
    python scripts_build_index/decrypt_dataset.py \
        --output data/decrypted.jsonl --generate-tsv data/queries.tsv
fi

# ---------------------------------------------------------------- 5. service
log "Installing supervisor service and portal entry"
sed -e "s|__INTERNAL_PORT__|${INTERNAL_PORT}|g" \
    "${BUNDLE}/service/bcp-retrieval.sh" > /opt/supervisor-scripts/bcp-retrieval.sh
chmod +x /opt/supervisor-scripts/bcp-retrieval.sh
cp "${BUNDLE}/service/bcp-retrieval.conf" /etc/supervisor/conf.d/bcp-retrieval.conf

python - "$EXTERNAL_PORT" "$INTERNAL_PORT" <<'PYEOF'
import sys
import yaml

external, internal = int(sys.argv[1]), int(sys.argv[2])
path = "/etc/portal.yaml"
with open(path) as fh:
    cfg = yaml.safe_load(fh) or {}
cfg.setdefault("applications", {})["BrowseComp Retrieval"] = {
    "hostname": "localhost",
    "external_port": external,
    "internal_port": internal,
    "open_path": "/docs",
    "name": "BrowseComp Retrieval",
}
with open(path, "w") as fh:
    yaml.safe_dump(cfg, fh, sort_keys=False)
print(f"portal.yaml: external {external} -> internal {internal}")
PYEOF

supervisorctl reread && supervisorctl update
supervisorctl restart caddy >/dev/null

log "Waiting for the service to load models (2-3 minutes)"
for _ in $(seq 1 60); do
    if ss -tlnp 2>/dev/null | grep -q "127.0.0.1:${INTERNAL_PORT}"; then break; fi
    sleep 10
done

# ---------------------------------------------------------------- 6. verify
log "Verifying"
supervisorctl status bcp-retrieval || true
PORT_VAR="VAST_TCP_PORT_${EXTERNAL_PORT}"
# Corpus integrity first: no GPU, no ranking, so a failure here is unambiguous.
if ! curl -sf --max-time 60 "http://127.0.0.1:${INTERNAL_PORT}/get_document?docid=16659" \
     | grep -q "1870s"; then
    echo "FAILED: docid 16659 is not the expected document -- wrong or partial corpus."
    exit 1
fi
echo "corpus OK (docid 16659 resolves)"

if curl -sf --max-time 300 -X POST "http://127.0.0.1:${INTERNAL_PORT}/search" \
     -H 'Content-Type: application/json' \
     -d '{"query": "who invented the telephone"}' \
     | python -c "import json,sys; h=json.load(sys.stdin); assert len(h)==10, h; \
print('top-10 docids:', [d['docid'] for d in h])"; then
    echo "OK: retrieval returned 10 ranked results."
    echo "    Compare against the reference top-10 in REPLICATE.md section 7."
    echo
    echo "  BASE_URL = http://${PUBLIC_IPADDR:-<ip>}:${!PORT_VAR:-<mapped-port>}"
    echo "  TOKEN    = \$OPEN_BUTTON_TOKEN"
else
    echo "FAILED. Check: tail -50 /var/log/portal/bcp-retrieval.log"
    exit 1
fi
