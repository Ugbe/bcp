# Atom Neutron dense retrieval service

The "perfect retriever" for BrowseComp-Plus upper-baseline runs.
It serves the dense API that the runner uses with `BCP_RETRIEVAL_API=dense`: `POST /search`, `GET /document/{docid}`, `POST /documents`, `GET /health`, `GET /info`.

**It is an oracle.**
The `browsecomp-overfit` encoder is `Qwen/Qwen3-Embedding-0.6B` plus the LoRA `CrowtherLabs/Atom-Neutron-emb-0.6b`, trained on all 830 benchmark queries and their qrels.
Every score it produces is an upper bound and must never be reported as held-out BrowseComp performance.
The same service also serves the unmodified base encoder (`model: "Qwen/Qwen3-Embedding-0.6B"`) as a non-oracle control.

## What it loads

| Artifact | Source | Size |
| --- | --- | --- |
| Base encoder | `Qwen/Qwen3-Embedding-0.6B` | 1.2 GB |
| Query-side LoRA (private) | `CrowtherLabs/Atom-Neutron-emb-0.6b` | 160 MB |
| Document index (100,195 x 1024) | `Tevatron/browsecomp-plus-indexes`, `qwen3-embedding-0.6b/` | 411 MB |
| Corpus text | `Tevatron/browsecomp-plus-corpus` | 1.7 GB |

The adapter is query-side only: the document tower was frozen during training, so the official prebuilt index, encoded with the unmodified base model, is the right index for both encoders.
No corpus encoding is needed.

## GPU

The service itself needs about 2 GB of GPU memory, and the index search runs on the CPU in milliseconds.
Policy also requires a Kev reranker (about 22 GB), so put both on one GPU:

- **RTX 5090 (32 GB): recommended.** About 25 GB used, 7 GB headroom.
- L40S or A6000 (48 GB): most headroom.
- RTX 4090 (24 GB): enough for this service alone, too tight with Kev.

Also plan for about 40 GB of disk and 32 GB of RAM (the corpus text is held in memory).

## Setup

```bash
git clone https://github.com/Ugbe/bcp.git /workspace/bcp && cd /workspace/bcp
HF_TOKEN=<token with CrowtherLabs access> bash neutron-retrieval/setup.sh
bash neutron-retrieval/run_server.sh
```

`setup.sh` installs a CUDA 12.8 PyTorch build, checks that it has kernels for this GPU with a real bf16 operation, and downloads the four artifacts.
`run_server.sh` starts the service in a tmux session named `neutron`, bound to `127.0.0.1:18200`, waits for it, and prints `/info`.

## Verify before any benchmark

Generate the benchmark queries once (`bash scripts/remote/bootstrap.sh` does it), then run the recall gate against the local service:

```bash
.venv/bin/python scripts_evaluation/verify_dense_retrieval.py --url http://127.0.0.1:18200 \
  --model browsecomp-overfit --k 5 --min-evidence 0.80 --min-gold 0.94
```

Expected with k=5: evidence recall 0.8105 (ceiling 0.8128) and gold recall 0.9497 (ceiling 0.9683).
With `--model Qwen/Qwen3-Embedding-0.6B`, evidence recall should be about 0.065.
Much lower overfit numbers mean the encoding convention below was broken, or the index is not the `qwen3-embedding-0.6b` one.

These numbers use the benchmark questions verbatim.
The agent writes its own search queries, and the adapter's model card measured about 0.87 evidence recall over the union of top-5 results on real agent sub-queries, and 0.59 set recall in a 20-question agent run.
Top-5 per search therefore does not guarantee that the model sees every evidence document; measure it from each run's `retrieved_docids`.

## Encoding convention (do not change)

- The query prefix `Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:` is concatenated with **no separator**.
- The tokenizer's own special tokens are used; do **not** append `eos_token` (see `bcp-replication/REPLICATE.md` §5.1 for how much that costs).
- Left padding, last-token pooling, L2 normalization, 512-token query limit.
- Documents carry no prefix; they are already in the index.

## Kev on the same GPU

Install and start Kev as in the AgentIR retrieval handoff (Kev repo at commit `2855ba2a55a80579176a459f78b95d03548cabb5`, `uv sync --extra serve`, plus `flash-linear-attention`), then:

```bash
tmux new -d -s kev "cd /workspace/kev && HF_HUB_OFFLINE=0 HF_HOME=/workspace/huggingface .venv/bin/python -m kev.serve --run jaredpalmer/kev-4b --port 8009 2>&1 | tee /workspace/kev.log; exec bash"
until curl -sf localhost:8009/v1/models >/dev/null; do sleep 5; done
```

## Expose both through the Vast portal

Pick two free container ports (`vast-capabilities | jq -c '.instance.open_ports[] | select(.in_use==false)'`), then:

```bash
RET_EXT=<free port 1>; KEV_EXT=<free port 2>
/venv/main/bin/python -c "import yaml; d=yaml.safe_load(open('/etc/portal.yaml')) or {'applications':{}}; \
d['applications']['Neutron Retrieval']={'hostname':'localhost','external_port':$RET_EXT,'internal_port':18200,'open_path':'/docs','name':'Neutron Retrieval'}; \
d['applications']['Kev Reranker']={'hostname':'localhost','external_port':$KEV_EXT,'internal_port':8009,'open_path':'/v1/models','name':'Kev Reranker'}; \
yaml.safe_dump(d, open('/etc/portal.yaml','w'), sort_keys=False)"
supervisorctl restart caddy
echo "BCP_RETRIEVAL_URL=http://$PUBLIC_IPADDR:$(printenv VAST_TCP_PORT_$RET_EXT)"
echo "BCP_KEV_URL=http://$PUBLIC_IPADDR:$(printenv VAST_TCP_PORT_$KEV_EXT)"
```

Both use the instance's `$OPEN_BUTTON_TOKEN` as the bearer token.
Check that an unauthenticated request returns 401 before handing out the URLs.

## Runner settings

```dotenv
BCP_RETRIEVAL_URL=http://<ip>:<retrieval port>
BCP_TOKEN=<OPEN_BUTTON_TOKEN>
BCP_RETRIEVAL_API=dense
BCP_RETRIEVAL_MODEL=browsecomp-overfit
BCP_KEV_URL=http://<ip>:<kev port>
BCP_KEV_TOKEN=<OPEN_BUTTON_TOKEN>
SEARCH_K=5
RETRIEVAL_NOVELTY=off
```
