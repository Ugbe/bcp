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

The default stack uses two runner threads, 40 productive calls, 64 iterations,
full `get_document`, server/local-compatible novelty handling, and context
compaction inside a 131,072-token window. Existing completed `run_qid_*.json`
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

The dashboard is on `http://127.0.0.1:${DASHBOARD_PORT:-7860}/`; expose it only
through a deliberate SSH/Vast port forward. Never put bearer, Azure, model, or
Hugging Face credentials in shell history, logs, or the repository.

## Separate retrieval service

If the 5090 is dedicated to the model, set `BCP_RETRIEVAL_URL` and `BCP_TOKEN`
to the retrieval box. The `bcp-replication/` bundle is a separate deployment
with its own GPU/disk requirements and its own `AGENT.md`; do not start it on
the model GPU unless VRAM and port capacity have been explicitly checked.
