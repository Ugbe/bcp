# Rebuilding the BrowseComp-Plus Retrieval Service

Everything needed to recreate this service on a fresh GPU instance. `/workspace` on the
original box was **not** a mounted volume, so a `recycle` or `destroy` wipes the
container filesystem — this bundle is the recovery path.

Nothing here is irreplaceable except the bundle itself: every model, index and corpus
is pulled from Hugging Face at setup time.

---

> **Driving this with an AI agent?** Start with `AGENT.md` — it is the ordered
> procedure, the pre-flight checks and the verification gates. This file is the
> reference behind it: what each artifact is, and every way the rebuild fails quietly.

## 1. Quick start

```bash
unzip bcp-retrieval-replication.zip
cd bcp-replication
./setup.sh                      # reads hf_token.env from the bundle
# or, with a rotated token:
HF_TOKEN=hf_xxxxxxxx ./setup.sh
```

~30-35 minutes: ~25 downloading ~26 GB of weights and indexes, then a few minutes
building the rerank token cache. The script is idempotent — re-run it after a failure
and it skips what already completed.

When it finishes:

```bash
curl -s -X POST "http://$PUBLIC_IPADDR:$VAST_TCP_PORT_10100/retrieve" \
  -H "Authorization: Bearer $OPEN_BUTTON_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "who invented the telephone"}'
```

A correct rebuild returns 10 hits. See §7 for what to compare them against — the
one-line "expect docid 16659 at rank 1" check that used to live here was measured on
the pre-cascade build and **no longer holds**; 16659 is a real corpus document but it
does not survive the two-stage reranker into the top 10.

---

## 2. What you must supply

| Requirement | Notes |
| --- | --- |
| **`HF_TOKEN`** | Mandatory — the two fine-tuned adapters are in **private** repos, and without it setup fails with a confusing `401 / RepositoryNotFound` on `adapter_model.bin`. **It ships in this bundle as `hf_token.env`**, which `setup.sh` reads automatically. That makes the zip itself a live credential: treat it as a secret, and rotate the token on Hugging Face if it is ever shared or leaves your control. An explicit `HF_TOKEN=...` in the environment overrides the file, so a rotated token needs no edit. |
| **GPU ≥ 24 GB** | Weights are ~17.5 GB (8B encoder + 0.6B reranker), but the stage-1 rerank batch pushes the **measured peak to 23.6 GB** of the RTX 4090's 24 GB. That is the verified configuration and it has little headroom. Below 24 GB, lower `--rerank-token-budget` (32768 → 16384) before first start. |
| **~45 GB disk** | 26 GB models in `HF_HOME` + 5 GB indexes (2.1 BM25 / 1.6 dense / 1.4 rerank token cache) + corpus + working room. |
| **A free external port** | Default `10100`. Confirm with `vast-capabilities \| jq '.instance.open_ports[]\|select(.in_use==false)'` and override via `EXTERNAL_PORT=...` if taken. |

The instance must be a Vast.ai image with supervisor + Caddy (`/opt/supervisor-scripts`,
`/etc/portal.yaml`). On a plain box, skip step 5 of `setup.sh` and run the server directly.

---

## 3. What this bundle contains

```
AGENT.md                                  ordered runbook + verification gates (start here)
setup.sh                                  one-command rebuild
hf_token.env                              ** live HF credential — keep this zip private **
requirements.txt                          pinned, verified-compatible versions
retrieval_api.md                          the API contract + measured quality
searcher/searchers/hybrid_searcher.py     ** the core work — does not exist upstream **
searcher/searchers/__init__.py            registry: adds `hybrid`, makes tevatron optional
searcher/search_r1_server.py              upstream + a --host flag
scripts_build_index/download_all.py       fetches every artifact
scripts_build_index/build_rerank_token_cache.py   pre-tokenizes the corpus (see 5.7)
scripts_evaluation/eval_retrieval.py      stage-by-stage recall
scripts_evaluation/tune_pipeline.py       candidate-pool sweep, one rerank pass per query
scripts_evaluation/ab_rerank.py           A/B rerank strategies on one candidate pool
scripts_evaluation/bench_concurrency.py   latency under N concurrent callers
notebooks/test_retrieval.ipynb            client-side API tests
service/bcp-retrieval.sh|.conf            supervisor service
```

Upstream is `github.com/texttron/BrowseComp-Plus` pinned at
`046949032b0328319cc9a02663a759ec601d9402` (2026-05-28). `setup.sh` clones that exact
commit and copies these files over it. **If you re-point at a newer upstream, re-check
`search_r1_server.py` and `searchers/__init__.py`, which are modified copies.**

### Upstream did not ship this pipeline

The API doc describes BM25 + dense + RRF + reranking. Upstream provides only `bm25`,
`faiss`, `reasonir` and an unimplemented `CustomSearcher` stub. `hybrid_searcher.py` is
the whole pipeline and is the one file you cannot regenerate from the repo.

---

## 4. Remote artifacts

| Artifact | Repo |
| --- | --- |
| Bi-encoder base | `Qwen/Qwen3-Embedding-8B` |
| Bi-encoder LoRA (private) | `DanielTobi0/qwen3-embedding-8b-browsecomp-lora-v2` |
| Cross-encoder base | `Qwen/Qwen3-Reranker-0.6B` |
| Cross-encoder LoRA (private) | `DanielTobi0/qwen3-reranker-0.6b-browsecomp-lora-v3` |
| Dense FAISS index (20 shards) | `DanielTobi0/browsecomp-plus-qwen3-embedding-8b-finetuned-index-v2` |
| BM25 Lucene index | `Tevatron/browsecomp-plus-indexes` (`bm25/*`) |
| Corpus (100,195 docs) | `Tevatron/browsecomp-plus-corpus` |

**The index and the adapter are a matched pair.** Query embeddings must come from the
same adapter that encoded the corpus. Pairing the v2 index with the v1 adapter (or with
the plain base model) silently degrades results rather than erroring.

---

## 5. Traps that cost real time

These are the things that will bite you on a rebuild. Each was found by measurement, not
by reading code.

### 5.1 Do not append an EOS token when encoding queries

The single highest-impact detail. The encoder's tokenizer already terminates a query with
`<|endoftext|>` (151643) under `add_special_tokens=True`, and **that** is the position the
index was pooled at. But `tokenizer.eos_token` is a *different* token, `<|im_end|>`
(151645). Appending it — the natural reading of "eos pooling", and what Tevatron's encode
path does — pools from the wrong position:

| Query encoding | recall@100 |
| --- | --- |
| tokenizer default (correct) | **0.76** |
| with `eos_token` appended | 0.65 |

It degrades quietly; nothing throws. `hybrid_searcher._encode_query` has this pinned with
a comment — do not "fix" it.

### 5.2 Do not install the repo's `pyproject.toml`

It requires `tevatron`, `vllm` and `deepspeed`. They pull a conflicting torch and are
unnecessary — the hybrid searcher uses `transformers` + `peft` + `faiss` directly. Use
`requirements.txt`. `searchers/__init__.py` is patched so the tevatron-dependent
`faiss_searcher` degrades to a clear error instead of breaking the whole registry import.

### 5.3 Pyserini needs `JAVA_HOME`, explicitly

Supervisor services get no login shell, so `java` is not on PATH and BM25 fails at
startup. The wrapper script exports `JAVA_HOME` for this reason.

### 5.4 Keep FAISS on CPU

`faiss-cpu` is intentional. `faiss-gpu` would move ~1.6 GB of index onto a GPU that is
already ~17.5 GB full of model weights.

### 5.5 Rerank latency is dominated by tokenization and padding

Two fixes in `_rerank`, worth ~30x combined; the naive version took ~7 min/query.

* Some corpus documents exceed 100k tokens (p99 ≈ 195k). Tokenizing one in full only to
  truncate to 8,192 dominates the request. Cut the **raw string** first.
* Document lengths span two orders of magnitude (p10 ≈ 329, p90 ≈ 16k tokens). Batching in
  rank order pads short docs up to the longest in the batch and wastes ~46% of the
  compute. Batch **length-sorted**, under a token budget.

### 5.6 The private adapters need the token at import time

`hybrid_searcher.py` calls `load_dotenv()` on the repo's `.env` at module import, so the
server, the eval scripts and the notebook all authenticate without separate wiring.
`setup.sh` writes that `.env` from `$HF_TOKEN`.

### 5.7 The rerank token cache must be built, and its absence is silent

The service is launched with `--rerank-token-cache indexes/rerank-token-cache`. If that
directory is missing, `_load_token_cache` logs **one warning** and the server starts
anyway, re-tokenizing ~200 documents on every request — ~2.2 s of avoidable
single-threaded work per query, on a box with 256 idle cores.

`setup.sh` now builds it (`scripts_build_index/build_rerank_token_cache.py`, ~1.4 GB).
Confirm it actually loaded rather than assuming:

```bash
grep -i "token cache" /var/log/portal/bcp-retrieval.log
# want: "Rerank token cache loaded: 100195 documents, 1.38 GB"
# not:  "Rerank token cache ... missing; tokenizing per request"
```

The cache is also **refused** — same silent degradation — if it was built with a
different tokenizer, a shorter `--max-doc-tokens` than `--rerank-max-length`, or a
different document prefix. Rebuild it after changing the reranker adapter.

### 5.8 flash-attn is optional, and its absence is also silent

The service passes `--attn-implementation flash_attention_2`, but `_resolve_attn`
downgrades to `sdpa` with a warning if `flash_attn` will not import. Correct results
either way, lower throughput on sdpa. It is not in `requirements.txt` because the wheel
must match torch, CUDA and python exactly (the original box ran
`2.8.3+cu130torch2.12-cp312`) and a source build takes ~1 hour. `setup.sh` tries a
matching prebuilt wheel and continues without it. Check which one you got:

```bash
grep -i "flash-attn not importable" /var/log/portal/bcp-retrieval.log   # silent = flash attention is live
```

---

## 6. Configuration

Live settings are the flags in `/opt/supervisor-scripts/bcp-retrieval.sh`:

```
--candidates-k 100          # candidates from each leg (BM25, dense)
--rerank-depth 200          # rerank every fused candidate (union <= 200) down to top 10
--k 10                      # results returned
--snippet-max-tokens 512
--rerank-stage1-length 512  # cascade stage 1: score all 200 candidates cheaply
--rerank-stage1-keep 20     # cascade stage 2: rescore the survivors at full 8192
--rerank-token-cache ...    # see 5.7 -- missing cache costs ~2.2 s/query, silently
--rerank-token-budget 32768 # activation peak; the main OOM lever
--attn-implementation flash_attention_2   # see 5.8 -- falls back to sdpa
```

Useful knobs: `--disable-rerank`, `--disable-bm25`, `--disable-dense`,
`--rerank-token-budget` (lower it if you OOM), `--rrf-k`.

Edit, then `supervisorctl restart bcp-retrieval` (~3 min to reload models).

---

## 7. Verifying a rebuild

```bash
# 1. service came up
supervisorctl status bcp-retrieval          # RUNNING
tail -20 /var/log/portal/bcp-retrieval.log  # "Uvicorn running on http://127.0.0.1:17070"

# 2. correct checkpoints are live
ps aux | grep search_r1_server              # both LoRA repos should appear

# 3. corpus integrity — no GPU, no ranking, so a failure here is unambiguous
curl -s "http://127.0.0.1:17070/get_document?docid=16659" | head -c 120
# want: {"docid":"16659","text":"---\ntitle: 1870s – 1940s: Telephone ...

# 3b. optimizations actually engaged (see 5.7, 5.8) — both fail open and silently
grep -i "token cache\|flash-attn not importable" /var/log/portal/bcp-retrieval.log

# 4. ranking reproduces — results are deterministic, so compare docid-for-docid
curl -s -X POST http://127.0.0.1:17070/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"who invented the telephone"}' \
  | python -c "import json,sys; print([h['docid'] for h in json.load(sys.stdin)])"

# 5. auth edge rejects anonymous callers
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:10100/retrieve \
  -H 'Content-Type: application/json' -d '{"query":"x"}'    # expect 401

# 6. full client suite
jupyter nbconvert --to notebook --execute notebooks/test_retrieval.ipynb --output /tmp/out.ipynb
```

**Reference top-10 for check 4**, captured from the original build on 2026-08-19 (RTX
4090, flash-attn and token cache both live). Ranking is deterministic — two runs on the
same box return the identical list — so a rebuild should reproduce these closely:

| Query | Top-10 docids, in order |
| --- | --- |
| `who invented the telephone` | 27302, 43872, 23852, 66818, 55529, 97072, 63750, 28527, 50779, 33110 |
| `which chemical element was named after a village in scotland` | 42211, 88026, 61135, 10752, 84924, 94110, 43131, 67220, 48809, 15842 |

Read this as a signal, not an assertion: a different GPU or attention kernel can reorder
near-tied documents (the first four all score 1.0), and that is fine. A *mostly
overlapping* set means the pipeline is right. A **disjoint** set means something in §4 or
§5.1 is wrong — most likely the adapter and index no longer match.

Expected healthy numbers: **~4.5 s/query** solo (~3.9 s of it reranking), ~23.6 GB peak
VRAM, 10 results, concurrent requests serialized by the GPU lock. If you see ~7 s, check
5.7 — that is the shape of a missing token cache.

**Retrieval quality regression check** (needs `data/queries.tsv`, which `setup.sh`
generates):

```bash
python scripts_evaluation/tune_pipeline.py --num-queries 40 --seed 1
```

Reference numbers from the original build — recall@10 vs `qrel_golds.txt`. **These were
measured before the two-stage cascade reranker** (§6, `--rerank-stage1-*`) and were not
re-measured after it, so treat the reranked row as approximate. The dense row is
unaffected by the cascade and is the one to trust as a regression signal:

| Configuration | recall@10 |
| --- | --- |
| dense top-10, no reranker | 0.4204 |
| reranked, fused-200 pool (deployed) | 0.3897 |
| *candidate-pool ceiling* | *0.8332* |

If dense recall collapses toward zero, the query encoder and the index no longer match —
look at §5.1 first, then confirm the adapter/index versions in §4.

> Note carried over from the original build: the reranker did **not** beat the
> bi-encoder's own top-10 on this sample, and BM25 contributes almost nothing
> (recall@100 = 0.085) because BrowseComp queries are deliberately obfuscated. Only
> `qrel_golds.txt` was measured; `qrel_evidence.txt` is untested and is the fairer target
> for the reranker, whose training positives included evidence documents.

---

## 8. Making it survive the next recycle

This rebuild lives on container storage again. To make it durable, either:

* **Attach a volume** and place `BrowseComp-Plus/` (with `indexes/`) plus `HF_HOME` on it; or
* **Bake it into provisioning** — point `PROVISIONING_MANIFEST` at a manifest that runs
  `setup.sh`, and set `PORTAL_CONFIG` at instance creation so the portal entry exists at
  boot:

  ```
  PORTAL_CONFIG="localhost:10100:17070:/docs:BrowseComp Retrieval"
  ```

Keep this zip somewhere off-box. It is the only component not reproducible from a public
source.
