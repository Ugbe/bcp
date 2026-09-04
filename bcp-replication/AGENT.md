# Rebuild runbook — for the AI agent doing this

You are rebuilding the BrowseComp-Plus hybrid retrieval service on a **fresh** GPU
instance. The original box was destroyed; this bundle is the whole recovery path.

Read this file first, then `REPLICATE.md` for the reasoning behind each step. This file
is the ordered procedure and the stop conditions. `REPLICATE.md` §5 is the list of things
that go wrong quietly — skim it before you start, not after something looks off.

**The defining hazard of this rebuild: almost every failure mode is silent.** A wrong
adapter, a missing token cache, a missing flash-attn wheel, and an EOS token appended to
the query all produce a service that starts cleanly, returns 10 plausible-looking
results, and is wrong or 5x slower. Do not treat "it returned JSON" as success. Run the
gates in step 5.

---

## 0. Before you rent or provision anything

Confirm the target instance meets these, because two of them cannot be fixed later:

| Check | Command | Required |
| --- | --- | --- |
| GPU memory | `nvidia-smi --query-gpu=name,memory.total --format=csv` | **≥ 24 GB.** Measured peak is 23.6 GB. 22 GB is not enough at default settings — see §2 of REPLICATE.md for the `--rerank-token-budget` workaround. |
| Disk | `df -h /workspace` | **≥ 45 GB free.** |
| Free external port | `vast-capabilities \| jq '.instance.open_ports[]\|select(.in_use==false)'` | One normal (≤65535) port. Default is `10100`; if taken, pass `EXTERNAL_PORT=<free one>`. |
| Vast image with supervisor + Caddy | `ls /opt/supervisor-scripts /etc/portal.yaml` | Both exist. On a plain box, see §2 of REPLICATE.md. |
| `HF_TOKEN` | `test -s hf_token.env` | Ships **inside this bundle** as `hf_token.env`; `setup.sh` reads it automatically. If it is missing, ask the user — the two adapters are in private repos, and without it setup fails ~10 minutes in at `download_all.py` with a `401`/`RepositoryNotFound` that reads like a network error. |

`hf_token.env` is a live credential. Do not print it, echo it into logs, paste it into a
message, or copy it anywhere outside this instance. If the user says it was rotated, run
setup with `HF_TOKEN=<new> ./setup.sh` — that overrides the file.

## 1. Run setup

```bash
cd bcp-replication
./setup.sh                               # add EXTERNAL_PORT=... if 10100 is taken
```

30-35 minutes. It is idempotent — if it dies, fix the cause and re-run it; it skips
completed downloads, the token cache and the decrypt step.

Do not "helpfully" `pip install -e .` or install the repo's `pyproject.toml`. It pulls
tevatron + vllm + deepspeed, which downgrade the preinstalled torch and break CUDA. This
is deliberate; see REPLICATE.md §5.2.

## 2. Expected non-fatal output

These lines are normal, not problems to fix:

* `NOTE: no matching flash-attn wheel (...)` — optional performance dependency, no wheel
  for this torch/CUDA/python combination. The service runs on sdpa. Correct, slower.
  Record it and tell the user; do not start a source build (~1 hour) unless asked.
* A warning that the GPU has less than 24 GB, if that is genuinely the case — but treat
  it as a real risk of CUDA OOM at first query, not noise.

## 3. Wait for the service properly

Model and index load takes **3-4 minutes** after the process starts.
`supervisorctl status bcp-retrieval` showing `RUNNING` does **not** mean it is accepting
connections — supervisor reports `RUNNING` once the process survives `startsecs`.

Poll the port, not the status:

```bash
until curl -sf http://127.0.0.1:17070/docs >/dev/null; do sleep 10; done
```

## 4. Verify — run all of these

Run every gate. Each one catches a different silent failure. Details in REPLICATE.md §7.

```bash
# a. corpus integrity (no GPU, unambiguous)
curl -s "http://127.0.0.1:17070/get_document?docid=16659" | head -c 120
#    want text beginning: ---\ntitle: 1870s – 1940s: Telephone

# b. the two silent optimizations
grep -i "token cache" /var/log/portal/bcp-retrieval.log
#    want: "Rerank token cache loaded: 100195 documents, 1.38 GB"
#    "missing; tokenizing per request" means the cache did not build -> ~2.2s/query lost
grep -i "flash-attn not importable" /var/log/portal/bcp-retrieval.log
#    silence = flash attention is live; a hit = running on sdpa

# c. correct checkpoints are actually loaded
ps aux | grep search_r1_server | grep -o "DanielTobi0/[^ ]*"
#    want both: qwen3-embedding-8b-browsecomp-lora-v2, qwen3-reranker-0.6b-browsecomp-lora-v3

# d. ranking reproduces (deterministic; compare to the table in REPLICATE.md §7)
curl -s -X POST http://127.0.0.1:17070/search -H 'Content-Type: application/json' \
  -d '{"query":"who invented the telephone"}' \
  | python -c "import json,sys; print([h['docid'] for h in json.load(sys.stdin)])"

# e. latency is in the right range (want ~4.5s; ~7s points at gate b)
time curl -s -X POST http://127.0.0.1:17070/retrieve -H 'Content-Type: application/json' \
  -d '{"query":"who invented the telephone"}' >/dev/null

# f. the auth edge rejects anonymous callers
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:10100/retrieve \
  -H 'Content-Type: application/json' -d '{"query":"x"}'      # want 401
```

**How to judge gate (d):** the reference lists in REPLICATE.md §7 are a signal, not an
assertion. Near-tied documents (the top four all score 1.0) can reorder on different
hardware or a different attention kernel. Mostly-overlapping = correct. **Disjoint =
broken**, and the cause is almost always a mismatched adapter/index pair (REPLICATE.md
§4) or an EOS token appended during query encoding (§5.1). Do not "fix" `_encode_query`.

## 5. Report back to the user

Give them, explicitly:

* `BASE_URL` — `echo "http://$PUBLIC_IPADDR:$VAST_TCP_PORT_10100"` (both parts change
  every rebuild; the external port is *not* the label `10100`)
* the auth token — `echo $OPEN_BUTTON_TOKEN`, sent as `Authorization: Bearer <token>`
* which gates passed, and **explicitly whether flash-attn and the token cache are live**,
  since those two decide whether they get ~4.5 s or ~7 s per query
* that `/workspace` is container storage unless a volume is attached — a recycle or
  destroy wipes this again. REPLICATE.md §8 covers making it durable.

Hand them `retrieval_api.md` as the client-facing API contract.

## 6. If you are asked to change the pipeline

Read REPLICATE.md §5 in full first. In particular:

* `hybrid_searcher.py` is the only file here with no upstream equivalent. It is the work.
* Changing the reranker adapter **invalidates the token cache** — rebuild it with
  `scripts_build_index/build_rerank_token_cache.py` or it will be silently refused.
* Changing the embedding adapter invalidates the **dense index**, which you cannot
  rebuild from this bundle; it is a matched pair pulled from Hugging Face.
* Measure with `scripts_evaluation/`: `eval_retrieval.py` (stage-by-stage recall),
  `tune_pipeline.py` (candidate-pool sweep), `ab_rerank.py` (rerank strategies on one
  fixed pool), `bench_concurrency.py` (latency under N callers).
