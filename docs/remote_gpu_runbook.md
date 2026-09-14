# Remote GPU runbook

This is the reproducible path for running the BrowseComp-Plus harness from a
fresh Linux GPU instance while a 5090 serves an OpenAI-compatible model. The
retrieval service is expected to be an authenticated HTTP service, normally on
a separate GPU; the local runner does not download or build retrieval indexes.

## Fresh clone

```bash
git clone https://github.com/Ugbe/bcp.git /workspace/bcp
cd /workspace/bcp
cp .env.example .env
chmod 600 .env
nano .env                         # fill in endpoint/token/deployment values
bash scripts/remote/bootstrap.sh
```

`bootstrap.sh` creates a lightweight `.venv`, installs
`requirements-remote.txt`, and generates `topics-qrels/queries.tsv` from the
public encrypted test set when it is absent. It intentionally does not install
the full root `pyproject.toml`; that dependency set includes torch/vLLM and
local retrieval packages that can overwrite a working CUDA image.

If the image already has vLLM, the standalone Qwythos model can be served on
the same instance. This deployment has no LoRA adapter or adapter/base-model
compatibility requirement.

```bash
bash scripts/remote/serve_vllm.sh
```

The launcher writes `runs/vllm.log` and `runs/vllm.pid`, refuses to start when
the configured port is already serving, waits for `/health`, and prints
`/v1/models`. If the image does not contain vLLM, use a vLLM-compatible image and install a version tested for its
CUDA/PyTorch stack; do not blindly run `pip install -e .`.

## Smoke gate and run

Run the full gate before starting the benchmark:

```bash
bash scripts/remote/smoke_test.sh
```

The gate checks authenticated retrieval and `get_document`, model PONG,
structured `search`, tool-result continuation, and Azure judge connectivity.
It exits nonzero on failure. For a live run with dashboard and judging:

```bash
bash scripts/remote/start_stack.sh
tmux attach -t "bcp-${RUN_NAME}"
```

The stack opens four tmux windows: dashboard, evaluator, benchmark, and stats.
The evaluator window prints only judged counts while it runs.
Live accuracy and recall are in the `stats` window; switch to it with Ctrl+B then W, or `tmux select-window -t "bcp-${RUN_NAME}:stats"`.
It refreshes every `MONITOR_INTERVAL` seconds (default 30) and shows run and judge progress, accuracy over judged, completed-only, and full-target denominators, judged and live evidence recall, tool calls per run, throughput, ETA, and the latest judged qids.
It warns when no run record has appeared for 15 minutes or when a run has waited that long for the judge, which usually means the benchmark or evaluator window has died.

To watch a run without the stack, or after reattaching to an older session that has no stats window:

```bash
bash scripts/remote/monitor.sh                       # uses RUN_NAME from .env
RUN_NAME=my-run bash scripts/remote/monitor.sh --once # single snapshot
tmux new-window -t "bcp-${RUN_NAME}" -n stats "bash scripts/remote/monitor.sh; exec bash"
```

The monitor reads run and eval files directly and needs only the Python standard library, so it works before `bootstrap.sh` has created `.venv`.

The default stack uses two runner threads, 12,000 output tokens, 32 productive
calls, 64 iterations, full `get_document`, 512-token snippets, evidence notes,
fresh final synthesis, optional multi-query/deep-pool tools, server/local-
compatible novelty handling, and context compaction inside a 131,072-token
window. Existing completed `run_qid_*.json`
files are skipped, so reconnecting and rerunning the same stack resumes safely.
Use a new `RUN_NAME` for each treatment arm. Stop only the named stack:

```bash
bash scripts/remote/stop_stack.sh
```

For a canary or a custom treatment, run the individual launcher after exporting
overrides in `.env`:

```bash
RUN_NAME=canary-20 MAX_TOOL_CALLS=24 MAX_ITERATIONS=40 \
  bash scripts/remote/run_benchmark.sh
```

For the ensemble treatment, use a separate output directory and launcher:

```bash
ENSEMBLE_RUN_NAME=qwythos-phase1-ensemble4 \
  bash scripts/remote/run_ensemble.sh
```

This writes one aggregate record per qid while retaining all four rollout
records and the pooled vote in diagnostics. Evaluate that directory with the
same `run_evaluator.sh` process, using the matching `RUN_NAME`.

The dashboard is on `http://127.0.0.1:${DASHBOARD_PORT:-7860}/`; expose it only
through a deliberate SSH/Vast port forward. Never put bearer, Azure, model, or
Hugging Face credentials in shell history, logs, or the repository.

## Separate retrieval service

If the 5090 is dedicated to the model, set `BCP_RETRIEVAL_URL` and `BCP_TOKEN`
to the retrieval box. The `bcp-replication/` bundle is a separate deployment
with its own GPU/disk requirements and its own `AGENT.md`; do not start it on
the model GPU unless VRAM and port capacity have been explicitly checked.
