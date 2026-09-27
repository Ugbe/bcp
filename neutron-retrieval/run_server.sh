#!/usr/bin/env bash
# Start the Neutron retrieval service in a tmux session and wait until it answers.
# Binds 127.0.0.1 by default; expose it only through an authenticating proxy, or set
# NEUTRON_API_TOKEN so the service checks a bearer token itself.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${NEUTRON_VENV:-/workspace/neutron-venv}"
INDEX_DIR="${NEUTRON_INDEX_DIR:-${NEUTRON_INDEX_ROOT:-/workspace/neutron-index}/qwen3-embedding-0.6b}"
HOST="${NEUTRON_HOST:-127.0.0.1}"
PORT="${NEUTRON_PORT:-18200}"
SESSION="${NEUTRON_TMUX_SESSION:-neutron}"
LOG="${NEUTRON_LOG:-/workspace/neutron-server.log}"
export HF_HOME="${HF_HOME:-/workspace/huggingface}"
# setup.sh downloaded everything; offline mode loads the private adapter from the
# cache without a token and keeps startup independent of the Hub. It is set on the
# server command only: an exported value would become the tmux server's environment
# when this creates the first session, and every later session (e.g. Kev, which
# must download its models) would inherit offline mode.
OFFLINE="${HF_HUB_OFFLINE:-1}"

if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
  echo "Something already answers on port ${PORT}; refusing to start a second server." >&2
  exit 1
fi
if tmux has-session -t "=${SESSION}" 2>/dev/null; then
  echo "tmux session ${SESSION} exists (still loading?). Watch ${LOG} or kill the session first." >&2
  exit 1
fi

tmux new-session -d -s "${SESSION}" \
  "HF_HUB_OFFLINE='${OFFLINE}' HF_HOME='${HF_HOME}' '${VENV}/bin/python' '${HERE}/neutron_server.py' --index-dir '${INDEX_DIR}' --host '${HOST}' --port '${PORT}' 2>&1 | tee '${LOG}'"
echo "Started tmux session ${SESSION}; log ${LOG}"

# Corpus, index, and model load take about a minute (longer on the first start).
for _ in $(seq 1 120); do
  if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    echo "Ready on ${HOST}:${PORT}"
    auth=()
    [[ -n "${NEUTRON_API_TOKEN:-}" ]] && auth=(-H "Authorization: Bearer ${NEUTRON_API_TOKEN}")
    curl -fsS "${auth[@]}" "http://127.0.0.1:${PORT}/info"; echo
    exit 0
  fi
  if ! tmux has-session -t "=${SESSION}" 2>/dev/null; then
    echo "The server exited during startup:" >&2
    tail -n 40 "${LOG}" >&2
    exit 1
  fi
  sleep 5
done
echo "Not ready after 10 minutes; the server keeps loading in tmux session ${SESSION}. Check ${LOG}." >&2
exit 1
