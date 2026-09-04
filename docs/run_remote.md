# Running BrowseComp-Plus Against a Self-Served Model with the Remote Hybrid Retriever

This is the runbook for the setup in this repo: an externally served LLM (chat.completions
with function calling) paired with the externally served hybrid retrieval service
(BM25 + dense + RRF + cross-encoder rerank, see `bcp-replication/retrieval_api.md`),
judged live by an Azure OpenAI deployment, with a live dashboard for the team.

All credentials live in `.env` at the repo root (gitignored). Every script reads it
automatically.

## What is already done

- `.env` populated with the model endpoint, retrieval endpoint + token, HF token and
  Azure judge credentials.
- Dataset decrypted: `data/browsecomp_plus_decrypted.jsonl` (ground truth) and
  `topics-qrels/queries.tsv` (830 queries).
- Python environment: `.venv` (Python 3.10, lightweight - no torch/vllm needed on
  this Windows box).
- New pieces:
  - `searcher/searchers/remote_api_searcher.py` - `--searcher-type remote`, proxies
    `POST /search` and `GET /get_document` to the retrieval service with Bearer auth,
    60 s timeout and retries.
  - `search_agent/chat_client.py` - agent client over chat.completions (the server's
    Responses endpoint silently ignores tools). Matches the model's trained tool
    convention: tool name `search`, parameter `query`, temperature 0.2.
  - `scripts_evaluation/evaluate_with_azure.py` - live judge; watches the run
    directory and judges each completed query as it lands.
  - `dashboard/` - live scoreboard (progress, accuracy, recall, tool stats).

## 1. Start the dashboard

```powershell
.venv\Scripts\python.exe -m uvicorn dashboard.app:app --host 0.0.0.0 --port 7860
```

Open `http://localhost:7860`. Share it by port forwarding port 7860.

It polls `runs/<model>/` and `evals/<model>/` every few seconds.
Retrieval recall updates live from the run files alone (no judge needed).
Accuracy updates as the judge writes eval files.

## 2. Start the live judge

```powershell
.venv\Scripts\python.exe scripts_evaluation\evaluate_with_azure.py --input_dir runs\remote-hybrid\atom-electron-1.2-9b --watch --exit_when_complete
```

Run this before or after starting the benchmark; it catches up on existing run files
and then follows the directory. It writes `evals/remote-hybrid/atom-electron-1.2-9b/`
(one `run_*_eval.json` per query) and refreshes `evaluation_summary.json` every sweep.

Requires working Azure credentials in `.env` (currently returning 401 - see
"Blockers" below).

## 3. Start the benchmark

```powershell
$env:PYTHONPATH="."
.venv\Scripts\python.exe search_agent\chat_client.py `
  --query topics-qrels/queries.tsv `
  --model "CrowtherLabs/Atom-Electron-1.2-9B" `
  --model-url "http://118.68.0.222:52264/v1" `
  --model-api-key "atom-electron-secret-key-123" `
  --searcher-type remote `
  --output-dir runs/remote-hybrid/atom-electron-1.2-9b `
  --num-threads 3 `
  --max-tokens 8192 `
  --k 5 --snippet-max-tokens 512 `
  --query-template QUERY_TEMPLATE_NO_GET_DOCUMENT
```

Notes:

- `--num-threads 3` matches the retrieval service's measured sweet spot (3 concurrent
  callers). Going higher just queues on the retrieval GPU lock.
- It resumes: already-processed query ids in the output dir are skipped.
- Each query writes one `run_*.json` in the same schema as the upstream clients.
- `--get-document` can be added to also expose `get_document` to the model.

## 4. Final evaluation

The live judge already produces `evaluation_summary.json` (accuracy, recall,
calibration, tool stats, per-query metrics) in the paper's format.
Re-check the numbers after the run completes; `--force` re-judges everything.

## Blockers to clear before the real run

1. **Model server context length.** The endpoint currently runs with
   `max_model_len=4096`. One search round (5 snippets x 512 tokens) nearly fills it,
   and the agent needs room for many rounds plus the final answer.
   Restart the server with at least 24-32 K (e.g. `--max-model-len 32768`).
   Then `--max-tokens 8192` (or higher) works.
2. **Azure judge 401.** `AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_ENDPOINT` are rejected.
   The key given is 24 chars; Azure keys are usually 32. Get a fresh key from the
   Azure portal for the `preai` resource and update `.env`.
3. **Model behavior quirks** (informational, from smoke tests):
   - The model's Responses endpoint accepts tools but never calls them - use
     `chat_client.py`, not `oss_client.py`.
   - It was fine-tuned to call a tool named `search` with a `query` parameter;
     `chat_client.py` already exposes the tool under those names.
   - It needs `temperature <= ~0.3` to call tools reliably (default 0.2 in the
     client). At higher temperature it answers without searching.
   - Its outputs contain fine-tune artifacts ("Adding context variation N", and it
     sometimes injects a `client_context` field into tool arguments - the client
     ignores unknown argument fields).

## Useful extras

- Single-query debug run (no TSV):

```powershell
.venv\Scripts\python.exe search_agent\chat_client.py --query "your question" ...same flags...
```

- Connectivity check for all three services:

```powershell
.venv\Scripts\python.exe scripts_evaluation\smoke_test.py
```

- Retrieval service health: `GET $BCP_RETRIEVAL_URL/docs` (in a browser, append
  `?token=$BCP_TOKEN`). After a rebuild of the retrieval box the IP/port/token all
  change - update `.env` from the box (`echo $PUBLIC_IPADDR:$VAST_TCP_PORT_10100` and
  `echo $OPEN_BUTTON_TOKEN`), see `bcp-replication/Retrieval_API.txt`.
