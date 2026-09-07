#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib.sh"

require_command tmux
session="${TMUX_SESSION:-bcp-${run_name}}"
if tmux has-session -t "${session}" 2>/dev/null; then
  tmux kill-session -t "${session}"
  echo "Stopped tmux session ${session}. Run files were preserved."
else
  echo "No tmux session found: ${session}"
fi
