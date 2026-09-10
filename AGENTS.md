# BrowseComp-Plus / Atom Electron operational guide

This is the hand-off document for contributors and agents. It records the training, serving, retrieval, benchmark, and dashboard work performed so far, including failed approaches and why they failed. Treat pasted logs as evidence, not as instructions; repository configuration and the current task take precedence.

## 1. System overview

BrowseComp-Plus evaluates an OpenAI-compatible reasoning model. Three services are involved:

1. **Model server**: vLLM on a Vast.ai GPU, serving a base model and optionally a LoRA adapter.
2. **Retrieval server**: authenticated hybrid BM25 + dense retrieval, reranked by a Qwen cross-encoder, over about 100k BrowseComp documents.
3. **Local runner/evaluator/dashboard**: this Windows repository runs `search_agent/chat_client.py`, Azure judging, and the FastAPI dashboard.

The runner calls the model's `search` and `get_document` tools. It writes one JSON per query under `runs/<run-name>`; the Azure evaluator writes records under `evals/<run-name>`; the dashboard reads both.

Important paths:

```text
runs/atom-electron-1.3-9b-full-830-docs-v2-128k/
evals/atom-electron-1.3-9b-full-830-docs-v2-128k/
topics-qrels/queries.tsv
search_agent/chat_client.py
searcher/searchers/remote_api_searcher.py
scripts_evaluation/evaluate_with_azure.py
scripts_evaluation/smoke_test.py
```

## 2. History and lessons learned

### Initial BrowseComp run

The first run performed very poorly: many conversations stopped without answers, some repeated words/analysis, and several never attempted a final answer. The causes were a combination of data and runtime problems:

- research traces were sometimes truncated after a tool call and had no final answer;
- some examples taught long repetitive reasoning and inconsistent contracts;
- the model was not always given `get_document`, so it could not inspect a promising hit in full;
- the runner could exceed the context budget after many searches and retry an impossible request;
- the dashboard could display stale or malformed placeholder HTML;
- local processes died whenever the Windows PC powered off;
- remote Vast services could independently disappear or change IP/port.

### Dataset and LoRA

The production dataset became `final_multitask_train.jsonl`, containing identity, instruction, and research/tool-use examples. Research traces must end with an actual answer, not merely `</think>` followed by a tool call. Exact BrowseComp parents used for training must not be used for unbiased evaluation.

The validated adapter was trained against `Qwen/Qwen3.5-9B` and published as `CrowtherLabs/Atom-Electron-1.3-9B`. A later attempted revision was named `Atom-Electron-1.4-9B`. The successful run used one epoch, learning rate `2e-5`, batch size 1, gradient accumulation 4, LoRA rank/alpha 16, and max sequence length 32,768. Preflight reported 710 train examples and 32 validation examples; the longest train example was close to 32k. The loss curve was noisy but not itself evidence of failure; evaluation loss was about 0.58.

### Training failures

- **Processor/chat-template error**: Qwen3.5's processor expects structured content items. Passing plain strings caused `TypeError: string indices must be integers`. The training script was patched to normalize content and preflight tokenization.
- **Assistant-only loss error**: Unsloth classified Qwen3.5 as vision-language and rejected `assistant_only_loss=True`. The working script sets `assistant_only_loss=False`; this was required for training and does not change the base model.
- **GGUF attempt**: `empero-ai/Qwen3.8-9B-Distill-GGUF` is a GGUF inference repository. Unsloth/Transformers could not load it because it lacks normal Transformers `config.json` metadata and is not a PEFT adapter. This failure is expected.

Do **not** apply the Atom Electron adapter to Empero/Qwen3.8. It was trained against different Qwen3.5 weights. Even matching tensor shapes do not make the adapter valid. For Qwen3.8, obtain a non-GGUF Transformers checkpoint and train a fresh adapter. GGUF is for inference, generally via llama.cpp.

### Serving and GPU failures

An earlier RTX 5090 instance exposed an old NVIDIA driver (`found version 12080`). The replacement was healthy:

```text
RTX 5090, driver 580.105.08, CUDA 13.0, compute capability (12, 0), PyTorch 2.13.0+cu130
```

The Vast vLLM template may start a base service on `127.0.0.1:18000`, with Caddy exposing port `8000`. A model does not appear in `/v1/models` until it is launched with the desired adapter. Never start a second vLLM process on an occupied port.

### Retrieval migration

The current authenticated retrieval API is:

- `POST /retrieve` with `{"query":"..." }`;
- response `{"result":[{"document":{"title","text"},"docid":"...","score":...}]}`;
- `GET /get_document?docid=...` returns full text;
- every request needs `Authorization: Bearer <BCP_TOKEN>`;
- exactly 10 hits are returned; title may be extremely long for title-less documents and must be truncated client-side.

`searcher/searchers/remote_api_searcher.py` now calls `/retrieve`, normalizes the wrapped response, uses nested document text, and authenticates `get_document`. `scripts_evaluation/smoke_test.py` was updated too. Keep exactly one current `BCP_TOKEN` in `.env`; duplicate entries previously caused an old token to override the new one and produce 401.

### Runner fixes and interruptions

The runner now registers both tools, records reasoning between tool calls, suppresses exact duplicate searches/documents, truncates fetched documents with `--document-max-tokens` (normally 4096), and retries once with thinking disabled when output is exhausted without an action.

A preflight exposed a context defect: after eight searches, input reached 24,577 tokens while the client reserved 8,192 output tokens, exceeding the 32,768 context. Keep context at least 128k when available, or dynamically reserve output and cap document/snippet lengths.

The target is 830 queries. The run was interrupted repeatedly by Windows power loss. After cleanup, 201 completed run/eval records remained active; post-restart files were moved to:

```text
runs/_quarantine_after_restart_20260829-165733/
```

Completed files are skipped on resume.

## 3. Environment

Use a local, uncommitted `.env` with current values:

```dotenv
MODEL_BASE_URL=http://<CURRENT_MODEL_PUBLIC_IP>:<PORT>/v1
MODEL_API_KEY=<MODEL_KEY>
MODEL_NAME=Atom-Electron-1.3-9B
BCP_RETRIEVAL_URL=http://<CURRENT_RETRIEVAL_PUBLIC_IP>:<PORT>
BCP_TOKEN=<CURRENT_RETRIEVAL_BEARER_TOKEN>
AZURE_OPENAI_API_KEY=<AZURE_KEY>
AZURE_OPENAI_API_VERSION=2024-10-21
```

Never commit or document real Hugging Face, retrieval, or Azure tokens. Tokens pasted into chat should be revoked and replaced.

## 4. Training on Vast (Qwen3.5 + fresh LoRA)

Use a current Unsloth/PyTorch image and compatible driver. Upload the training bundle into `/workspace`. This is the successful command shape:

```bash
export HF_TOKEN='<NEW_HF_TOKEN>'

python /workspace/run_training_v2_qwen35_eval_oom_fix.py \
  --model-name Qwen/Qwen3.5-9B \
  --train-file /workspace/training_multitask_v3_hf/data/training_multitask_v3/final_multitask_train.jsonl \
  --validation-file /workspace/training_multitask_v3_hf/data/training_multitask_v3/final_multitask_validation.jsonl \
  --output-dir /workspace/atom-electron-multitask-v3 \
  --max-seq-length 32768 \
  --epochs 1 \
  --learning-rate 2e-5 \
  --batch-size 1 \
  --gradient-accumulation 4 \
  --lora-r 16 \
  --lora-alpha 16 \
  --push-to-hub \
  --hub-model-id CrowtherLabs/Atom-Electron-1.3-9B
```

Before resuming, verify a complete checkpoint contains adapter and trainer files. If no complete checkpoint exists, use a fresh output directory. After training, verify the Hub repo has `adapter_config.json`, adapter weights, tokenizer/config metadata, and the exact base model recorded.

## 5. Serving the validated Qwen3.5 adapter

If the template owns port 18000, stop its vLLM process before restarting:

```bash
vllm serve Qwen/Qwen3.5-9B \
  --host 127.0.0.1 \
  --port 18000 \
  --dtype bfloat16 \
  --language-model-only \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.92 \
  --enable-lora \
  --max-lora-rank 16 \
  --lora-modules '{"name":"Atom-Electron-1.3-9B","path":"CrowtherLabs/Atom-Electron-1.3-9B","base_model_name":"Qwen/Qwen3.5-9B"}' \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_xml \
  --reasoning-parser qwen3
```

If Caddy proxies 8000 to 18000, Windows uses `http://<VAST_IP>:8000/v1`; otherwise expose the selected port. Verify:

```bash
curl http://127.0.0.1:18000/v1/models
curl -s http://127.0.0.1:18000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"Atom-Electron-1.3-9B","messages":[{"role":"user","content":"Reply with exactly: PONG"}],"temperature":0,"max_tokens":32,"chat_template_kwargs":{"enable_thinking":false}}'
```

The response should be only `PONG` (surrounding whitespace is acceptable to the smoke test), and structured tool calls must also work.

## 6. Serving Empero/Qwen3.8 GGUF

This is a separate inference experiment. The GGUF card recommends llama.cpp:

```bash
llama serve -hf empero-ai/Qwen3.8-9B-Distill-GGUF:Q4_K_M
```

Do not attach `Atom-Electron-1.3-9B`. To LoRA-train Qwen3.8, use its non-GGUF Transformers checkpoint and a newly trained adapter.

## 7. Smoke test and benchmark

From PowerShell in the repository root:

```powershell
.venv\Scripts\python.exe scripts_evaluation\smoke_test.py
```

Do not start a full run unless retrieval search plus `get_document`, model plain response, structured tool call, tool-result continuation, and Azure judging all pass.

Start the dashboard:

```powershell
$env:BCP_TARGET_QUERIES='830'
.venv\Scripts\python.exe -m uvicorn dashboard.app:app --host 0.0.0.0 --port 7860
```

Start the evaluator:

```powershell
.venv\Scripts\python.exe scripts_evaluation\evaluate_with_azure.py `
  --input_dir runs\atom-electron-1.3-9b-full-830-docs-v2-128k `
  --query_file topics-qrels\queries.tsv `
  --watch --exit_when_complete --poll_interval 15
```

Start/resume the benchmark:

```powershell
.venv\Scripts\python.exe search_agent\chat_client.py `
  --query topics-qrels\queries.tsv `
  --model $env:MODEL_NAME `
  --model-url $env:MODEL_BASE_URL `
  --model-api-key $env:MODEL_API_KEY `
  --searcher-type remote `
  --output-dir runs\atom-electron-1.3-9b-full-830-docs-v2-128k `
  --num-threads 2 `
  --max-tokens 4096 `
  --max-iterations 40 `
  --max-tool-calls 24 `
  --temperature 0.6 `
  --top-p 0.95 `
  --top-k 20 `
  --repetition-penalty 1.05 `
  --k 10 `
  --snippet-max-tokens 256 `
  --document-max-tokens 4096 `
  --get-document `
  --query-template QUERY_TEMPLATE `
  --verbose
```

The runner skips existing `run_qid_*.json` files. Keep the same output directory to resume. Confirm the log reports the expected skip count. Move records to a dated quarantine directory before any destructive cleanup.

For a new compaction-enabled higher-budget canary, use a fresh output directory
(do not mix its records with the audited 24-call run):

```powershell
.venv\Scripts\python.exe search_agent\chat_client.py `
  --query topics-qrels\queries.tsv `
  --model $env:MODEL_NAME `
  --model-url $env:MODEL_BASE_URL `
  --model-api-key $env:MODEL_API_KEY `
  --searcher-type remote `
  --output-dir runs\atom-electron-1.3-9b-full-830-docs-v2-128k-compaction40 `
  --num-threads 2 `
  --max-tokens 4096 `
  --max-iterations 64 `
  --max-tool-calls 40 `
  --context-compaction `
  --context-window-tokens 131072 `
  --context-compaction-trigger-tokens 98304 `
  --context-compaction-keep-tool-rounds 2 `
  --context-compaction-max-tokens 1536 `
  --context-compaction-reserve-tokens 8192 `
  --temperature 0.6 `
  --top-p 0.95 `
  --top-k 20 `
  --repetition-penalty 1.05 `
  --k 10 `
  --snippet-max-tokens 256 `
  --document-max-tokens 4096 `
  --get-document `
  --query-template QUERY_TEMPLATE_RESEARCH_LEDGER `
  --verbose
```

Run the 24-call compaction control in a separate directory first, changing only
`--max-tool-calls 24` and keeping `--max-iterations 40`. Compare it with the
40-call directory on the same fixed canary questions before considering a full
830-query launch. `--num-threads` and the model/retrieval endpoints should remain
the same across both arms.

## 8. Monitoring and resilience

Run dashboard, evaluator, and benchmark in separate PowerShell windows or with `Start-Process` and redirected logs. Record parent PIDs. Check `http://localhost:7860/` and its status API. Terminate only recorded PIDs; do not kill unrelated Python processes.

For long runs, use a persistent supervisor (Task Scheduler/service/persistent terminal) locally and `tmux` or `screen` on Vast. A PC outage kills local processes even if the remote model survives; a Vast rebuild can kill model/retrieval services while local processes keep retrying.

Diagnose in this order:

1. Model `/v1/models` and plain PONG.
2. Authenticated retrieval `/retrieve` and `/get_document`.
3. Benchmark stderr for context overflow, 401/404, refusal, or repeated retries.
4. Run/eval counts and dashboard status.
5. Only then restart or quarantine records.

## 9. Quality cautions

- Keep BrowseComp training parents separate from held-out evaluation.
- Require a final answer in every research training trace.
- Preserve reasoning/tool events for postmortems.
- Evaluate tool use, completion, retrieval recall, and exact-answer accuracy; do not rely on one loss curve.
- Retrieval scores near 1.0 are often tied; ranking order matters more than tiny score gaps.
- Snippets are capped server-side; `get_document` is full text. Truncate titles/documents client-side.
- Keep concurrency modest (two benchmark threads); retrieval requests queue on one GPU.
- Treat all IPs, ports, model IDs, and tokens as ephemeral configuration.

## 10. Required working practice for agents and contributors

`AGENTS.md` is a living project context file, not a one-time historical note. After **every substantive step**—configuration change, code edit, training attempt, service restart, benchmark launch/stop, dataset change, or diagnosis—update this file before handing work back.

Each update should record, as applicable:

- the date/time and what was attempted;
- the exact files, directories, commands, endpoints, model IDs, and run names involved (use placeholders for secrets);
- the observed result, including warnings, errors, counts, PIDs, and relevant metrics;
- whether the step succeeded, failed, was reverted, or remains in progress;
- the next safe action and any blocker or user action required;
- compatibility assumptions that future agents must not silently change.

Do not claim a service, run, or training job is healthy without a direct check. Prefer recording smoke-test output, file counts, logs, or API responses. When a remote IP, port, token, model revision, or checkpoint changes, update the environment/configuration section and the relevant operational command immediately.

Before beginning work, read this file and inspect the current repository state. Before finishing work, re-read the affected sections and bring them up to date. Keep a concise chronological record for new incidents rather than deleting old lessons. Never put credentials in `AGENTS.md`; use names such as `<CURRENT_RETRIEVAL_BEARER_TOKEN>` and store actual values only in the local uncommitted `.env` or the remote secret manager.


## 11. Copy-paste Vast.ai setup: Qwen3.8 Transformers base + its newly trained LoRA

This is the valid path when the new adapter was trained against the non-GGUF Transformers checkpoint `empero-ai/Qwen3.8-9B`. Replace the two Hub IDs below with the exact IDs from the successful training run. Do not use the GGUF repository as the vLLM LoRA base.

### 11.1 Fresh instance checks

Paste these commands into the new Vast terminal:

```bash
set -e
nvidia-smi
python -c "import torch; print(torch.__version__); print(torch.cuda.get_device_name(0)); print(torch.cuda.get_device_capability(0))"
```

For an RTX 5090, the driver should be in the R570+ family and PyTorch should report CUDA 12.8/13.x support. If `nvidia-smi` reports an old driver or PyTorch cannot initialize CUDA, destroy/recreate the instance with a newer NVIDIA image; installing only the CUDA toolkit inside the container will not repair an old host driver.

### 11.2 Install/update runtime packages

If the Vast image already contains a working vLLM build, skip the upgrade and proceed to the model download check. Otherwise paste:

```bash
python -m pip install --upgrade pip
python -m pip install --upgrade vllm huggingface_hub
python -c "import vllm; print('vLLM', vllm.__version__)"
```

Do not blindly mix a CUDA 12.x PyTorch wheel with a CUDA 13.x host image. Keep the image's tested PyTorch/vLLM combination unless vLLM explicitly requires an upgrade.

### 11.3 Authenticate and verify the base and adapter repositories

Use a newly created, least-privilege Hugging Face token. Never paste a real token into `AGENTS.md`, git, or chat:

```bash
export HF_TOKEN='<NEW_HF_TOKEN>'
export BASE_MODEL='empero-ai/Qwen3.8-9B'
export ADAPTER_REPO='CrowtherLabs/<QWEN38_ADAPTER_REPO>'
export SERVED_NAME='Atom-Electron-Qwen3.8-9B'
```

Log in and verify that both repositories are accessible:

```bash
huggingface-cli login --token "$HF_TOKEN" --add-to-git-credential
python - <<'PY'
import os
from huggingface_hub import HfApi
api = HfApi(token=os.environ["HF_TOKEN"])
for repo in (os.environ["BASE_MODEL"], os.environ["ADAPTER_REPO"]):
    info = api.model_info(repo)
    print(repo, "OK", info.id)
PY
```

Verify that the adapter repository contains `adapter_config.json` and adapter weight files:

```bash
python - <<'PY'
import os
from huggingface_hub import HfApi
api = HfApi(token=os.environ["HF_TOKEN"])
repo = os.environ["ADAPTER_REPO"]
files = [x.rfilename for x in api.list_repo_tree(repo, recursive=True) if hasattr(x, "rfilename")]
print("\n".join(files))
assert any(x.endswith("adapter_config.json") for x in files), "No adapter_config.json: this is not a PEFT LoRA repository"
assert any("adapter_model" in x for x in files), "No adapter weights found"
PY
```

Inspect the adapter configuration and confirm its `base_model_name_or_path` is the same Qwen3.8 Transformers base:

```bash
python - <<'PY'
import json, os
from huggingface_hub import hf_hub_download
p = hf_hub_download(os.environ["ADAPTER_REPO"], "adapter_config.json", token=os.environ["HF_TOKEN"])
cfg = json.load(open(p))
print(json.dumps(cfg, indent=2))
print("adapter base:", cfg.get("base_model_name_or_path"))
print("expected base:", os.environ["BASE_MODEL"])
```

If the adapter says `Qwen/Qwen3.5-9B`, stop: that is the old Atom Electron adapter and must not be applied to Qwen3.8.

### 11.4 Start vLLM on public port 10100

If a template process is already using the desired GPU/port, identify it before starting another server:

```bash
ps -ef | grep -E '[v]llm|[p]ython'
ss -ltnp | grep -E ':10100|:18000|:8000'
```

Stop only the known template vLLM PID if necessary:

```bash
kill <KNOWN_VLLM_PID>
```

Start the Qwen3.8 Transformers base with the newly trained adapter. This binds directly to port 10100:

```bash
nohup vllm serve "$BASE_MODEL" \
  --host 0.0.0.0 \
  --port 10100 \
  --dtype bfloat16 \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.92 \
  --enable-lora \
  --max-lora-rank 16 \
  --lora-modules "{\"name\":\"$SERVED_NAME\",\"path\":\"$ADAPTER_REPO\",\"base_model_name\":\"$BASE_MODEL\"}" \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_xml \
  --reasoning-parser qwen3 \
  > /workspace/vllm-qwen38.log 2>&1 &
echo $! | tee /workspace/vllm-qwen38.pid
```

Use `tmux` if installed so the process survives terminal disconnects:

```bash
tmux new -s qwen38
vllm serve "$BASE_MODEL" --host 0.0.0.0 --port 10100 --dtype bfloat16 --max-model-len 32768 --gpu-memory-utilization 0.92 --enable-lora --max-lora-rank 16 --lora-modules "{\"name\":\"$SERVED_NAME\",\"path\":\"$ADAPTER_REPO\",\"base_model_name\":\"$BASE_MODEL\"}" --enable-auto-tool-choice --tool-call-parser qwen3_xml --reasoning-parser qwen3 2>&1 | tee /workspace/vllm-qwen38.log
# Detach with Ctrl-B then D; reattach with: tmux attach -t qwen38
```

Do not use both the `nohup` and `tmux` launches. Choose one.

### 11.5 Wait for readiness and verify the adapter

```bash
for i in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:10100/health >/dev/null; then echo READY; break; fi
  sleep 5
done
curl -s http://127.0.0.1:10100/v1/models | python -m json.tool
```

The models response should list both the base model and the adapter name. Test plain output:

```bash
curl -s http://127.0.0.1:10100/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$SERVED_NAME\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: PONG\"}],\"temperature\":0,\"max_tokens\":32,\"chat_template_kwargs\":{\"enable_thinking\":false}}" | python -m json.tool
```

Test the tool contract:

```bash
curl -s http://127.0.0.1:10100/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$SERVED_NAME\",\"messages\":[{\"role\":\"user\",\"content\":\"Search for the browsecomp tool contract test.\"}],\"tools\":[{\"type\":\"function\",\"function\":{\"name\":\"search\",\"description\":\"Search the corpus\",\"parameters\":{\"type\":\"object\",\"properties\":{\"query\":{\"type\":\"string\"}},\"required\":[\"query\"]}}}],\"tool_choice\":\"auto\",\"temperature\":0,\"max_tokens\":256}" | python -m json.tool
```

A plain request must return only `PONG`; the tool request must produce a structured `search` call. If either fails, inspect `/workspace/vllm-qwen38.log` and do not expose the model to the benchmark.

### 11.6 Point the Windows runner at port 10100

Set the new public endpoint in the local uncommitted `.env`:

```dotenv
MODEL_BASE_URL=http://<VAST_PUBLIC_IP>:10100/v1
MODEL_API_KEY=<MODEL_KEY_IF_CONFIGURED>
MODEL_NAME=Atom-Electron-Qwen3.8-9B
```

Run the repository smoke test from PowerShell:

```powershell
.venv\Scripts\python.exe scripts_evaluation\smoke_test.py
```

The smoke test must pass model, authenticated retrieval, and Azure judge checks before starting BrowseComp. The benchmark command in section 7 can then be reused, with `--model` matching `MODEL_NAME` and the same run directory policy.

### 11.7 GGUF warning

Do not substitute `empero-ai/Qwen3.8-9B-Distill-GGUF` for `BASE_MODEL` in the vLLM LoRA command. GGUF is a quantized inference artifact and is not the Transformers/PEFT base expected by the adapter. If you only want the GGUF without LoRA, use llama.cpp:

```bash
llama serve -hf empero-ai/Qwen3.8-9B-Distill-GGUF:Q4_K_M
```

That is a separate server and cannot validate the newly trained Transformers LoRA adapter.

## 13. Latest benchmark launch — 2026-08-31

The configured endpoints passed the full smoke test: retrieval search and
`get_document`, model PONG, structured tool call, tool-result continuation, and
Azure judge all succeeded. The active configuration was:

```text
MODEL_BASE_URL=http://180.189.55.43:42630/v1
MODEL_NAME=Atom-Electron-1.3-9B
BCP_RETRIEVAL_URL=http://83.50.45.8:40700
```

The first hidden benchmark launcher exited immediately because PowerShell does not
automatically export values from `.env`; `--model` therefore received no argument.
The corrected launch passed model name, URL, and API key explicitly. Dashboard,
evaluator, and benchmark are running with these recorded parent PIDs:

```text
dashboard  19660
evaluator  15136
benchmark  34100
```

Benchmark log:

```text
runs/atom-electron-1.3-9b-full-830-docs-v2-128k-benchmark-20260831-233848-rerun2.out.log
```

Initial benchmark state: `629` remaining queries, skipping `201` existing query
records. The dashboard is available at `http://localhost:7860/`. If relaunching from
PowerShell, either import `.env` explicitly or pass the model arguments as explicit
CLI values; do not rely on `$env:MODEL_NAME` being populated by `python-dotenv` in a
separate child process.

## 12. Latest serving incident — 2026-08-31

An attempted vLLM launch on port `10100` used the validated Qwen3.5 configuration:
`Qwen/Qwen3.5-9B` with `CrowtherLabs/Atom-Electron-1.3-9B`, `bfloat16`, LoRA enabled,
`qwen3_xml` tool parsing, `qwen3` reasoning parsing, `--max-num-seqs 2`, and
`--max-model-len 131072`. vLLM 0.28.0 failed before model loading because the
instance reported `[Errno 101] Network is unreachable` while requesting
`https://huggingface.co/Qwen/Qwen3.5-9B/resolve/main/config.json`, and the files were
not present in the local cache. No inference service became healthy. Next safe action:
restore outbound Hugging Face access or pre-download both the base model and adapter
into the instance cache, then relaunch and verify `/v1/models`, the PONG request,
structured tool calling, and the full smoke test before resuming BrowseComp. The
131,072-token context setting may require a later GPU-memory check; it was not the
cause of this failure. The user's local internet access does not establish that the
remote Vast container has outbound connectivity. `[Errno 101] Network is unreachable`
most strongly indicates a missing route/default gateway, blocked container egress, a
temporary Vast networking fault, or an IPv6-only routing problem; it is less consistent
with a bad Hugging Face token, which would normally produce an HTTP 401/403.


## 14. Network-failure cleanup and resume — 2026-09-01

The previous benchmark attempt produced 830 run files, including 322 non-completed records caused by network timeouts/refusals. Representative failed records had statuses such as `incomplete_request_error`, `incomplete_no_final_answer`, `incomplete_length`, or `incomplete`; they were not valid benchmark results. The 508 records with status `completed` were retained.

The failed run records were moved, not permanently deleted, to:

```text
runs/_quarantine_network_failure_20260901-132900/runs/
```

Forty-two matching incomplete evaluation records and the old evaluation summary were moved to:

```text
runs/_quarantine_network_failure_20260901-132900/evals/
```

After cleanup, the active directory contains 508 completed run records and 499 evaluation records; the evaluator will fill the missing evaluations as it watches.

Before resuming, the full smoke test passed all three services:

```text
retrieval /retrieve and /get_document: OK
model PONG, structured tool call, continuation: OK
Azure judge: OK
```

The resumed processes are:

```text
dashboard PID: 19660
evaluator PID: 15420
benchmark PID: 22132
```

The benchmark log is:

```text
runs/atom-electron-1.3-9b-full-830-docs-v2-128k-benchmark-20260901-133335.out.log
```

It reports `322` remaining queries and skips `508` completed queries. The `transformers PyTorch was not found` line is a local runner warning; the runner uses the remote OpenAI-compatible model and the process remained alive after the warning. Continue monitoring the benchmark log and dashboard. If another network incident occurs, stop the runner, classify records by the JSON `status` field, quarantine only non-completed records, rerun smoke tests, and resume from the completed set.


## 15. Completion check — 2026-09-01

A process check after the resumed network-recovery run found no benchmark or evaluator process alive. The dashboard remains active:

```text
dashboard PID: 19660
benchmark/evaluator: stopped
```

All 830 run and 830 evaluation filenames exist, but filename counts alone do not prove completion. Status inspection found:

```text
run records:
  completed: 805
  incomplete_length: 4
  incomplete_no_final_answer: 20
  incomplete_request_error: 1

evaluation records:
  is_completed=true: 805
  is_completed=false: 25
```

The evaluation summary currently reports 52.77% accuracy and 58.27% retrieval recall, but those figures include only the 805 completed responses; the 25 incomplete queries require a deliberate rerun or must be reported as incomplete. Do not call the 830-query run fully complete until those 25 records are either successfully rerun and reevaluated or explicitly excluded/documented.

## 16. Full-run forensic audit — 2026-09-02

A read-only forensic audit of
`runs/atom-electron-1.3-9b-full-830-docs-v2-128k` and its matching eval
directory was completed with
`scripts_analysis/analyze_atom_electron_run.py`. The detailed report and derived
CSV/JSON tables are in:

```text
analysis/atom-electron-1.3-9b-830-audit/REPORT.md
analysis/atom-electron-1.3-9b-830-audit/summary.json
analysis/atom-electron-1.3-9b-830-audit/per_run_metrics.csv
```

Verified counts: 830 unique qids (sparse IDs from 1 through 1266), 805 completed,
20 `incomplete_no_final_answer`, four `incomplete_length`, and one
`incomplete_request_error`. The request error was qid 134; its benchmark log
records `Request timed out.` The 25 evaluator `parse_error=true` records are the
same 25 unfinished runs; there were zero completed Azure judge responses with a
format-parser failure and zero active `incomplete_malformed_tool_call` statuses.

The score is 438/830 = 52.77%, or 438/805 = 54.41% among completed runs. Manual
review found 21 high-confidence judge false negatives and 12 additional probable
or alias-policy-dependent false negatives. Conservatively correcting the 21 gives
459/830 = 55.30%; accepting all 12 additional cases gives 471/830 = 56.75%.
Qid 791 is a directly observed cross-run inconsistency: `JadaL` versus `Jadal`
was accepted by an earlier judge and rejected in this run.

Exactly 128 runs reached the 24-tool limit: 18 correct, 58 committed wrong
answers, 31 explicit no-answer/refusal finals, and 21 unfinished. Thus 52
budget-exhausted runs produced no usable answer and 110 were not correct. Do not
raise the budget without a stagnation policy: the 24-call group scored only
14.06%, averaged 0.49 `get_document` calls, and contained 982 locally blocked
exact-duplicate searches.

The proposed seen-document novelty filter addresses a measured issue. Across
10,169 actual retrieval calls, 58,764/101,690 returned top-ten slots (57.79%)
were docids already shown earlier in the same conversation. There were 1,302
searches with zero novel documents and 5,070 searches with seven or more repeated
documents. Implement exclusions as structured per-qid API/harness state with
over-fetching, not prompt prose; keep old documents accessible through
`get_document` and A/B test a hard exclusion against an 8-new/2-anchor policy.

Retrieval deduplication is not sufficient by itself: 189 completed wrong runs had
at least 50% qrel evidence recall, 46 had 100% recall, and 130 contained the
literal normalized gold answer in tool output. The next safe work is: fix judge
normalization and status separation; add a temperature-zero emergency finalizer;
rerun the 25 incomplete qids; add duplicate/stagnation handling and structured
document exclusions; then run a fixed failure-stratified A/B plus an oracle-qrel
evidence test to distinguish retriever/harness limits from the LoRA/model ceiling.

## 17. Early-final / premature-stopping diagnosis — 2026-09-02

The forensic analyzer and report were extended to audit runs where the model
voluntarily finalized before the 24-tool ceiling:

```text
scripts_analysis/analyze_atom_electron_run.py
analysis/atom-electron-1.3-9b-830-audit/REPORT.md
analysis/atom-electron-1.3-9b-830-audit/early_stop_review.csv
analysis/atom-electron-1.3-9b-830-audit/per_run_metrics.csv
analysis/atom-electron-1.3-9b-830-audit/summary.json
```

Verified behavior in `search_agent/chat_client.py`: before 24 calls the runner
uses `tool_choice="auto"`; any valid formatted final response with
`finish_reason="stop"` is accepted as completed. The 24-call value is only a
ceiling. The current `QUERY_TEMPLATE` does not define evidence sufficiency,
material constraint coverage, or a pre-final verification rule.

Verified run counts: 698 completed responses stopped before 24 calls, of which
420 were judged correct and 278 wrong. Within those, 73 were explicit no-answer
finals (mean 16.49 calls, 548 unused calls in aggregate). A broad phrase screen
found 176 non-refusal candidate answers whose final reasoning/response admitted
an evidence gap; 106 were correct and 70 wrong, so caveat language alone must not
be used as a hard retry gate. Among the 70 wrong caveated candidates, 32 had at
least 50% qrel recall, 10 had 100% recall, and 27 had already received the literal
gold answer. Among the 73 early refusals, 26 had at least 50% recall and 16 had
already received the literal gold answer.

Training-data inspection found that the 94 early/middle/late decision views end
in tool calls and do teach continued research. However, each of the 94
`research_answer_synthesis` views chooses the first pair whose tool output contains
the gold-answer string (plus at most one prior pair) and appends the final answer;
the 20 recovered rows likewise synthesize from one evidence block. These 114
compressed finals are 54.8% of the 208 research rows that end in a final answer,
and their construction does not verify coverage of all identity-critical clues.

Next safe action: implement and A/B test a stateful early-final gate rather than a
minimum-call quota. Track material constraints and supporting docids; expose
remaining productive budget; reopen early refusals when calls remain; require one
targeted verification search or `get_document` only when a caveated candidate has
an unresolved identity-critical constraint; combine this with novelty/stagnation
metadata; and compare temperature 0/0.2 against the current 0.6. Retraining should
follow only after this test, with constraint-coverage-selected synthesis examples
and paired examples favoring a discriminating search over a premature final.

## 18. Harness, retrieval-novelty, and judge implementation — 2026-09-02

The implementation plan is:

```text
docs/atom_electron_run_improvement_implementation_plan.md
```

The local runner now has opt-in, per-qid research controls in
`search_agent/chat_client.py`: productive calls are counted separately from
locally rejected duplicates; `--early-final-guard` supports `off`, `refusal`, and
`conservative`; recovery is bounded and tries `tool_choice="required"` with a safe
`auto` fallback; low-novelty/repeated actions produce strategy guidance; novelty
searches carry per-qid seen docids; and `--emergency-finalizer` makes exactly one
temperature-zero, tools-disabled final attempt after an invalid forced final.
Run records may include a backward-compatible `diagnostics` object. Internal
controller keys are stripped before OpenAI API serialization. Seen snippet
documents remain retrievable through `get_document`.

The original query templates remain unchanged for a true A/B baseline. New
treatment templates in `search_agent/prompts.py` are:

```text
QUERY_TEMPLATE_RESEARCH_LEDGER
QUERY_TEMPLATE_RESEARCH_LEDGER_NO_GET_DOCUMENT
```

Retrieval novelty support was added to:

```text
searcher/searchers/base.py
searcher/searchers/remote_api_searcher.py
bcp-replication/searcher/search_r1_server.py
bcp-replication/searcher/searchers/hybrid_searcher.py
```

The upgraded `/retrieve` request accepts optional `exclude_docids`, `k`, and
`seen_anchor_count`, with 2,000-exclusion, 100-result, query-length, and docid-length
bounds. `HybridSearcher` over-fetches cheap BM25/dense candidates and filters seen
docids before expensive reranking. Responses include `novel_count`,
`repeated_count`, `excluded_count`, `candidate_pool_exhausted`, and
`exclusions_applied`. The local remote client retries a legacy request shape after
400/422 and otherwise filters the old fixed top ten locally; that fallback can
return fewer than ten results and is explicitly diagnosed.

`scripts_evaluation/evaluate_with_azure.py` now separates `run_status`,
`model_answer_parse_status`, `judge_call_status`, and `judge_parse_status`, while
preserving `judge_result.correct` and adding canonical `final_correct`. Safe
deterministic equivalences can bypass Azure; 21 reviewed equivalences and nine
reviewed rejections are qid/pair-scoped, while 12 policy-dependent cases remain
unresolved unless `--adjudicate_near_matches` is enabled. Model confidence is
preserved for deterministic decisions. An incomplete run is no longer called a
judge parse error, and a completed record lacking output is a model-answer parse
error.

New verification/diagnostic artifacts:

```text
tests/test_retrieval_novelty.py
tests/test_evaluate_with_azure.py
tests/fixtures/judge_equivalence_cases.csv
scripts_analysis/build_ab_diagnostic_set.py
topics-qrels/splits/atom_electron_failure_stratified_100.tsv
topics-qrels/splits/atom_electron_failure_stratified_100.manifest.json
```

The frozen A/B TSV contains 100 unique, non-overlapping qids: 20 early refusals,
20 wrong caveated candidates, 15 high-recall wrong, 15 duplicate/stagnation
failures, 10 zero-recall failures, 10 incomplete runs, and 10 correct caveated
regression controls.

Final local verification passed:

```text
Python compilation: passed for all changed Python modules
unittest discovery: 75/75 passed
historical analyzer: reproduced 438/830 (52.77%) and all prior counts
```

The local `transformers` warning that PyTorch is unavailable is expected for these
offline tests. A live `scripts_evaluation/smoke_test.py` call on 2026-09-02 found:

```text
Azure judge https://procureai.openai.azure.com: PONG OK
retrieval http://83.50.45.8:40700: connection actively refused
model http://180.189.55.43:42630/v1: connection error
```

No remote deployment, canary, evaluator rewrite of historical results, or new
benchmark was performed. Next safe action: restore the validated Qwen3.5/Atom
Electron model service; deploy the patched retrieval server/hybrid files; set
`BCP_REQUIRE_RETRIEVAL_EXCLUSIONS=1` and
`BCP_REQUIRE_TOOL_CHOICE_REQUIRED=1`; rerun the full smoke test; then run a fresh
10–20-qid canary and the frozen 100-qid A/B in new directories. Do not start the
830-query run or call server-side novelty healthy until those direct checks pass.

## 19. Speed-first serving recommendation — 2026-09-02

For the validated `Qwen/Qwen3.5-9B` plus
`CrowtherLabs/Atom-Electron-1.3-9B` LoRA, the recommended speed-first rental is a
single dedicated NVIDIA H200 SXM 141 GB for the model server. It provides ample
headroom for the roughly 19 GB BF16 model artifact, a 131,072-token configured
context, and several concurrently active sequences without tensor-parallel
communication overhead. Prefer SXM to PCIe when the marketplace distinguishes
them. An H100 SXM 80 GB is the value fallback; an RTX 5090 32 GB is not the
speed-first choice for this long-context, concurrent workload because its much
smaller memory leaves less KV-cache and batching headroom.

If price is irrelevant and a provider has a directly verified B200/vLLM image,
a single B200 180 GB is the raw-performance upgrade. It should be treated as an
optional canary candidate rather than the default because the 9B model does not
need its capacity and end-to-end benchmark speed can be dominated by retrieval,
network round trips, and sequential tool reasoning before model throughput uses
the extra hardware. Do not use two H100s or two consumer GPUs in tensor parallel
for this 9B model unless a measured canary beats one H200; the model fits on one
GPU and cross-GPU communication is unnecessary.

For the shortest whole-run wall time, keep model and retrieval on separate GPUs:

```text
model serving:       1x H200 SXM 141 GB
retrieval/reranking: 1x H100 80 GB (or two replicated retrieval workers if the
                     server/index implementation supports safe sharding/load balancing)
runner concurrency:  benchmark 2, 4, and 6 threads; promote the fastest setting
                     that does not increase timeouts, queue depth, or answer regressions
```

This follows from the forensic run, which issued 10,169 real retrieval calls and
used only two benchmark threads. Faster decoding cannot remove retrieval service
time. Before the 830-query rerun, execute the full smoke test and a fixed 20-qid
latency canary. Record model prefill/decode throughput, per-search retrieval and
reranker latency, p50/p95 end-to-end qid duration, GPU utilization/memory, queue
depth, and error rate at each thread count. Use `--max-num-seqs` at least as high
as the selected runner concurrency and retain `--max-model-len 131072` unless the
canary proves a smaller value covers every treatment trace. No GPU was rented or
remote service changed as part of this recommendation.

## 20. Loss-aware context compaction implementation — 2026-09-02 15:45 +01:00

An opt-in context-compaction controller was added to
`search_agent/chat_client.py` so a future treatment can test productive-tool
budgets above 24 without knowingly exceeding the 131,072-token serving window.
The detailed design, flags, A/B arms, and promotion checks were added to
`docs/atom_electron_run_improvement_implementation_plan.md` section 15.

Before every normal model request, the runner now estimates the serialized active
messages plus tool schemas. When enabled, it triggers at 75% of the configured
window by default (98,304 tokens for the recommended explicit setting) or earlier
if the normal output reservation and an 8,192-token safety margin require it. The
compactor is a temperature-zero, thinking-disabled, tools-disabled call and must
return a `COMPACTED RESEARCH LEDGER`. Its contract preserves candidate answers,
verified/contradicted/unresolved identity-critical constraints, supporting
docids, unopened promising documents, tried/failed query directions, and the next
discriminating action. Retrieved text is explicitly treated as untrusted evidence.

Compaction does not delete history. Replaced messages are marked locally with
`_compacted_out` and omitted only by `_messages_for_api`; the original assistant
tool calls and tool outputs remain available to `_normalize_chat_messages` and in
the persisted run record. The initial query and two recent tool rounds are retained
verbatim by default. Compactor calls do not consume productive-tool budget.
Diagnostics record successful/failed compactions, estimated before/after tokens,
messages compacted, and the productive-call position. Unsafe paths fail closed as
`incomplete_context_compaction_too_late`,
`incomplete_context_compaction_failed`,
`incomplete_context_compaction_unavailable`, or
`incomplete_context_after_compaction` instead of sending a known-oversized normal
request.

New CLI flags:

```text
--context-compaction
--context-window-tokens 131072
--context-compaction-trigger-tokens 98304
--context-compaction-keep-tool-rounds 2
--context-compaction-max-tokens 1536
--context-compaction-reserve-tokens 8192
```

`tests/test_chat_client.py` now verifies that compaction removes old payloads from
the next API request while preserving the complete audit/tool record, materially
reduces the estimated active context, and refuses a compactor request triggered
too late. Verification completed locally:

```text
py_compile: passed
unittest discovery: 77/77 passed
chat_client.py --help: passed
```

The existing `--model-api-key` argparse default was also changed from the resolved
environment secret to `None`, with environment resolution performed only after
argument parsing. This prevents `--help` from printing a configured API key while
preserving runtime behavior. No credential was written to this file.

No live model/retrieval service or production benchmark was available or changed.
Next safe action: include a compaction-only 24-call arm and an otherwise identical
40-call arm in the fixed canary/A/B. Use `--max-iterations 64` for the 40-call arm.
Promote the larger budget only if it adds useful novelty/verification without
increasing wrong committed answers, compaction failures, or context-limit errors.

## 21. Benchmark command update — 2026-09-02

The `Start/resume the benchmark` section now retains the historical 24-call
baseline command and adds a separate fresh-directory `compaction40` command with
`--max-tool-calls 40`, `--max-iterations 64`, and all context-compaction flags.
It also specifies a separate 24-call compaction control and requires comparing
both arms on the same fixed canary before any 830-query launch. No benchmark was
started by this documentation-only update.

## 22. Compaction40 power-loss recovery — 2026-09-02 21:03 +01:00

The PC powered off while the fresh run below was active:

```text
runs/atom-electron-1.3-9b-compaction40-5090-20260902/
evals/atom-electron-1.3-9b-compaction40-5090-20260902/
```

After reboot, no Python worker was alive and port 7860 had no listener. The run
directory contained 84 records: 79 `completed`, three
`incomplete_context_compaction_failed`, one `incomplete_length`, and one generic
`incomplete`. There were 84 matching evaluation records. The five incomplete
run records (qids 786, 796, 806, 814, and 851) and their five matching evals were
moved, not deleted, to:

```text
runs/_quarantine_power_loss_20260902-210249/runs/
runs/_quarantine_power_loss_20260902-210249/evals/
```

This left 79 completed runs and 79 evals active so all unfinished work, including
the five prior incomplete outcomes, will be retried. The pre-restart smoke test
passed retrieval `/retrieve` (10 hits), authenticated `get_document`, model PONG,
structured tool calls, `tool_choice="required"`, tool-result continuation, and
Azure judging. Verified active non-secret endpoints were:

```text
MODEL_BASE_URL=http://180.189.55.43:23295/v1
MODEL_NAME=Atom-Electron-1.3-9B
BCP_RETRIEVAL_URL=http://38.194.166.158:41396
```

The retrieval server reported that server-side novelty exclusions are unavailable.
This is a legacy-compatible warning for this run because its recovered command
keeps `--retrieval-novelty off`; do not claim the newer server-exclusion treatment
is deployed.

The dashboard, evaluator, and benchmark were relaunched as hidden processes at
21:03:20. Recorded launcher and Python child PIDs are:

```text
dashboard: launcher 15372, child 3012
evaluator: launcher 20136, child 9428
benchmark: launcher 14560, child 15520
```

The dashboard directly returned HTTP 200 at `http://127.0.0.1:7860/`. The
evaluator reported `79 runs, 79/830 queries judged`. The runner reported exactly
`751 remaining queries (skipping 79)`. Its child had established connections to
both model and retrieval endpoints, confirming the two worker streams were active.
The expected local warning that Transformers cannot find PyTorch remains harmless
because inference is remote.

Resume logs are:

```text
runs/dashboard-resume-20260902-210320.out.log
runs/dashboard-resume-20260902-210320.err.log
runs/atom-electron-1.3-9b-compaction40-5090-20260902-judge-resume-20260902-210320.out.log
runs/atom-electron-1.3-9b-compaction40-5090-20260902-judge-resume-20260902-210320.err.log
runs/atom-electron-1.3-9b-compaction40-5090-20260902-benchmark-resume-20260902-210320.out.log
runs/atom-electron-1.3-9b-compaction40-5090-20260902-benchmark-resume-20260902-210320.err.log
```

The exact recovered benchmark arm uses two threads, 4,096 output tokens,
64 iterations, 40 productive tool calls, `QUERY_TEMPLATE_RESEARCH_LEDGER`, full
`get_document`, and context compaction at 98,304 estimated input tokens within a
131,072-token window, retaining two tool rounds and reserving 8,192 tokens. It
keeps temperature 0.6, top-p 0.95, top-k 20, repetition penalty 1.05, 10 search
hits, 256-token snippets, and 4,096-token documents. Continue monitoring the new
run/eval counts and these exact PIDs; if another outage or endpoint failure occurs,
move only non-completed records to a new dated quarantine, rerun the full smoke
test, and resume in the same active directories.

## 23. Compaction40 stop and Vast no-credit quarantine — 2026-09-03 01:49 +01:00

The user reported that there is no remaining Vast.ai credit and asked to stop the
active compaction40 run and quarantine the incomplete results rather than resume.
The matching local processes were identified by command line and stopped:

```text
dashboard: launcher 15372, child 3012
evaluator: launcher 20136, child 9428
benchmark: launcher 14560, child 15520
```

The active directories were:

```text
runs/atom-electron-1.3-9b-compaction40-5090-20260902/
evals/atom-electron-1.3-9b-compaction40-5090-20260902/
```

At shutdown/cleanup, the run directory contained 420 run records: 191
`completed`, 226 `incomplete_request_error`, one generic `incomplete`, one
`incomplete_context_compaction_too_late`, and one `incomplete_length`. The 229
non-completed run records were moved, not deleted, to:

```text
runs/_quarantine_vast_out_of_credit_20260903-014901/runs/
```

Their matching evaluation records were moved to:

```text
runs/_quarantine_vast_out_of_credit_20260903-014901/evals/
```

Two incomplete run records had no evaluation file yet, consistent with work that
was still in flight or failed before judging. The stale `evaluation_summary.json`
was also moved into the quarantine evals folder because it described the mixed
pre-cleanup state. Final verification found no matching BrowseComp Python
processes alive, and the active run/eval folders each contain 191 records, all
run records with status `completed`. Do not restart this run until the model
serving budget/endpoints are intentionally restored and a fresh full smoke test
passes.

## 24. Evidence-note and fresh-final harness treatment — 2026-09-07 09:31 +01:00

Implemented the pasted two-stage research proposal in:

```text
search_agent/chat_client.py
tests/test_chat_client.py
```

The raw-snippet baseline remains unchanged unless the new opt-in flags are used.
`--evidence-notes` runs one isolated, tools-disabled, temperature-zero summarizer
call after each productive tool result. The planner receives the rigid evidence
note instead of raw snippets/documents; each tool message retains
`_raw_tool_output`, and persisted records additionally expose `raw_tool_outputs`
for audit. Notes may emit `CANDIDATE_TABLE_JSON`; the runner merges it into a
per-qid candidate table and injects a compact system-style checkpoint every
three productive calls. Opened documents and top snippets are retained in state
for later synthesis.

`--fresh-final-answer` runs a new tools-disabled final synthesis with thinking
enabled after a completed answer, or after the emergency finalizer is invoked.
Its prompt is capped by `--fresh-final-prompt-max-tokens` (default 24,000
approximate tokens) and prioritizes the candidate table, opened documents,
evidence notes, and the top three snippets. Both conversation and fresh answers,
status, citation detection, and promotion decision are persisted in diagnostics.
The fresh answer replaces the conversation answer only when it is valid,
disagrees, and cites evidence; otherwise the original remains the benchmark
answer.

New CLI controls are `--evidence-notes`, `--evidence-note-max-tokens`,
`--fresh-final-answer`, `--fresh-final-max-tokens`, and
`--fresh-final-prompt-max-tokens`. No live model, retrieval service, benchmark,
or production run was changed.

Verification completed locally:

```text
py_compile search_agent/chat_client.py: passed
chat_client.py --help: passed; all five new flags present
unittest discovery: 79/79 passed
```

The expected offline warning that Transformers cannot find PyTorch remains
harmless because the runner uses the remote OpenAI-compatible model. Next safe
action: restore and smoke-test the remote model/retrieval endpoints, then run a
small fixed canary comparing the raw baseline, evidence-note treatment, and
evidence-note plus fresh-final treatment before any larger benchmark.

## 25. Remote GPU clone-and-run packaging — 2026-09-07

The repository was packaged for a fresh Linux GPU clone that runs the
OpenAI-compatible BrowseComp harness beside a model server:

```text
.env.example
requirements-remote.txt
scripts/remote/{lib,bootstrap,prepare_queries,serve_vllm,smoke_test,run_benchmark,run_evaluator,start_stack,stop_stack}.sh
docs/remote_gpu_runbook.md
README.md
```

The remote profile is intentionally separate from `pyproject.toml`. It installs
only the runner, tokenizer, evaluator, dataset bootstrap, and FastAPI dashboard
dependencies, so it does not replace a Vast image's CUDA/PyTorch/vLLM stack.
`bootstrap.sh` creates `.venv`, installs `requirements-remote.txt`, and generates
the ignored `topics-qrels/queries.tsv` from the encrypted public test set when
needed. `serve_vllm.sh` serves the standalone
`CrowtherLabs/Qwythos-9B-Analyst` model, waits for health, records a PID/log, and
refuses to start over an occupied port. The stack launcher uses a named tmux
session for dashboard, Azure evaluator, and resumable benchmark processes.

Defaults match the current recommended treatment: two runner threads, 40
productive calls, 64 iterations, `get_document`, `QUERY_TEMPLATE_RESEARCH_LEDGER`,
server-compatible novelty requests with local fallback, and 131,072-token
context compaction. Each treatment must use a new `RUN_NAME`; rerunning the same
name resumes completed `run_qid_*.json` files. Secrets remain in the ignored
`.env` and are represented only by placeholders in committed files.

Local verification after packaging:

```text
py_compile: passed
chat_client.py --help: passed; context compaction, evidence notes, and fresh final flags present
unit tests: passed (unittest discovery; no failures)
git diff --check: passed
shell syntax check: unavailable in the managed Windows sandbox because bash/WSL process creation returned E_ACCESSDENIED; scripts use bash and should be run through bash on the target Linux instance
```

No live model, retrieval service, Azure judge, vLLM process, or production
benchmark was started by this packaging change. On the GPU, fill `.env`, run the
smoke gate, and start the named tmux stack only after all three endpoints pass.

## 26. Standalone Qwythos model serving update — 2026-09-07

The remote serving package was updated to use the standalone Hugging Face model
`CrowtherLabs/Qwythos-9B-Analyst`:

```text
BASE_MODEL=CrowtherLabs/Qwythos-9B-Analyst
MODEL_NAME=CrowtherLabs/Qwythos-9B-Analyst
RUN_NAME=qwythos-9b-analyst-remote
```

`scripts/remote/serve_vllm.sh` now requires only `BASE_MODEL`, `MODEL_NAME`, and
the vLLM host/port settings, passes `--served-model-name "$MODEL_NAME"`, and
does not enable LoRA or reference `ADAPTER_REPO`, `SERVED_MODEL_NAME`, or a
LoRA rank. The remote `.env.example` and runbook were updated accordingly. The
older Atom Electron/Qwen3.x adapter instructions elsewhere in this file are
historical records and are not the active remote clone-and-run configuration.

No live model or benchmark service was changed. The safe next action is to clone
the pushed commit, confirm the Qwythos repository is accessible from the GPU,
run `scripts/remote/serve_vllm.sh`, and require the full smoke test to pass
before starting the benchmark stack.

## 27. Live evaluation visibility diagnosis — 2026-09-07 21:09 +01:00

The user reported a live remote run named `qwythos-9b-analyst-remote` showing
`72/830` completed run records but `0/830` evaluations and `0` pending evaluations
in the remote dashboard. Read-only inspection of the current repository verified:

- `scripts/remote/start_stack.sh` starts dashboard, evaluator, and benchmark in
  separate tmux windows concurrently.
- `scripts/remote/run_evaluator.sh` invokes
  `scripts_evaluation/evaluate_with_azure.py --watch --exit_when_complete` with
  the same `${run_dir}` and `${eval_dir}` derived from `RUN_NAME`.
- `evaluate_with_azure.py` polls every 15 seconds, discovers `run_*.json`, and
  evaluates each file as it appears; it does not intentionally wait for all
  830 queries.
- `chat_client.py` writes each completed record through a temporary file and
  atomic `os.replace`, so completed records are safe to copy while the benchmark
  continues.

The screenshot therefore indicates an operational visibility/startup problem,
not the intended evaluation plan: the evaluator may have exited, may have a
different `RUN_NAME`/directory, or the dashboard may be pointed at a different
workspace. The remote benchmark writes to `/workspace/bcp/runs/qwythos-9b-analyst-remote`.
No local or remote process was changed. The safe migration is to stop only the
remote evaluator window, periodically copy completed run JSON files to a matching
local directory, and run the Azure evaluator locally against that mirror. Do not
run two evaluators against the same evaluation output directory, and do not copy
partial `.tmp` files. Required remote SSH host/port/key details remain user-side
configuration and must not be recorded here.

During this diagnosis, a concrete path bug was found in the remote launcher:
`run_evaluator.sh` passed the already run-specific `eval_dir` into
`evaluate_with_azure.py`, whose `mirror_directory_structure()` appends the run
name again. The launcher was corrected locally to pass the parent
`${REPO_ROOT}/evals` directory. The active Vast clone still needs this one-file
hotfix applied or the evaluator should be started manually with `--eval_dir
/workspace/bcp/evals`; no active remote process was changed by this local edit.

## 28. tmux benchmark-stats visibility — 2026-09-07

The user reported that after splitting
`bcp-qwythos-9b-analyst-remote:benchmark` with `/tmp/bcp_stats.py`, attaching to
the tmux session showed judge progress but not accuracy statistics. The likely
cause is tmux window selection: `split-window -t ...:benchmark` creates a pane
in the benchmark window but does not guarantee that a later plain
`attach-session -t <session>` selects that window. The stack creates dashboard,
evaluator, and benchmark as separate windows, with evaluator commonly remaining
the visible/current window. Safe checks are `tmux select-window -t
<session>:benchmark`, `tmux list-panes -t <session>:benchmark`, and
`tmux capture-pane` for the stats pane. No remote process or file was changed by
this diagnosis.

## 29. Sharing the remote terminal — 2026-09-07

The user asked for the password/token requested when sharing the Vast Jupyter
Terminal URL. This is an access-credential question, not a benchmark runtime
change. The likely credentials are distinct: Jupyter's own token can be found
with `jupyter server list`, while Vast's Instance Portal open-button credential
may be exposed as `OPEN_BUTTON_TOKEN`. Do not record either value here or paste
it into chat. A Jupyter Terminal provides arbitrary shell/code execution, so the
safer default for a research lead who only needs progress is a protected,
read-only dashboard URL rather than a terminal link. No remote service or local
file was changed.

## 30. Roadmap sections 4.3–4.6 implementation — 2026-09-10

The Phase 1 recommendations were implemented locally and remain opt-in at the
runner API boundary unless enabled by the updated remote `.env.example`:

```text
search_agent/research_ensemble.py
search_agent/chat_client.py
search_agent/prompts.py
searcher/searchers/remote_api_searcher.py
scripts/remote/run_ensemble.sh
scripts/remote/run_benchmark.sh
scripts_analysis/audit_zero_recall_retrieval.py
scripts_analysis/compare_phase1_ab.py
docs/phase1_ab_protocol.md
tests/test_phase1_treatments.py
```

The ensemble runner launches independent seeded rollouts with the default
temperature spread `0.2,0.6,0.6,0.8`, pools deduplicated raw evidence, runs a
fresh pooled final synthesis with weight two, and writes one evaluator-compatible
`run_qid_*.json` containing all rollout records and summed tool calls. It supports
N=1,2,4,8 through `--rollouts` and resumes completed aggregate records.

The remote searcher now supports client-side RRF for two-to-four query rewrites
and an opt-in `search_pool` request for deep retrieval. The `deep_search` tool
returns ranked docids, titles, and first-sentence previews in batches suitable
for evidence notes. Legacy servers that still return ten hits are explicitly
marked `pool_supported=false`; no top-100 claim is made in that case. The
constraint-first prompt and evidence-note controller require the first three
search actions to target distinct rare constraints.

Treatment defaults are now 12,000 output tokens, 32 productive calls,
temperature 0.25 for single runs, 512-token snippets, server novelty with two
anchors, evidence notes, fresh final synthesis, and optional multi-query/deep
pool flags. `scripts/remote/run_ensemble.sh` provides the four-rollout path.
`docs/phase1_ab_protocol.md` records the frozen dev/failure-100 A/B order and
comparison command. `scripts_analysis/audit_zero_recall_retrieval.py` audits
full-question and hand-written constraint retrieval ranks; `compare_phase1_ab.py`
compares Azure summary accuracy/recall/cost deltas.

Security cleanup: `.env.example` no longer contains the previously present
retrieval, Azure, or Hugging Face credential values; it contains placeholders.
The ignored local `.env` is the only place for live secrets. No remote model,
retrieval service, evaluator, or benchmark was started by this change.

Verification completed:

```text
python -m py_compile: passed for all changed Python modules
chat_client.py and research_ensemble.py --help: passed
unittest discovery: 83/83 passed
git diff --check: passed
bash syntax check: unavailable in managed Windows sandbox (E_ACCESSDENIED); run on Linux/Vast before launch
```

Next safe action: push this change, pull it into a fresh GPU/runner clone, fill
the placeholder `.env` locally, run the standard smoke gate, then run the fixed
single/deep-pool/ensemble A/B arms before a full benchmark. Do not reuse an old
run directory across treatment configurations.
