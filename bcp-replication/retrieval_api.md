# BrowseComp Retrieval — external access

Hybrid BM25 + dense retrieval over the BrowseComp-Plus corpus (100,195 docs), fused
with RRF and reranked by a Qwen3 cross-encoder. Returns top-10.

Reranking runs as a two-stage cascade: all ~200 fused candidates are scored cheaply
at 512 tokens, then the top 20 are rescored at the full 8192 tokens. Everything that
reaches the returned top-10 is still judged at full length.

## Endpoint

Derive it on the instance — the host port is not the label `10100`, and both the IP
and the port change on every rebuild:

```bash
echo "http://$PUBLIC_IPADDR:$VAST_TCP_PORT_10100"
```

## Auth

Every external request needs the instance token — `$OPEN_BUTTON_TOKEN` (or
`$WEB_PASSWORD`) from inside the container. Without it Caddy returns `401`. It is
issued per instance — read it with `echo $OPEN_BUTTON_TOKEN` on the rebuilt box.
Any one of:

```bash
-H "Authorization: Bearer $TOKEN"     # preferred
?token=$TOKEN                          # query param
-u "vastai:$TOKEN"                     # basic auth
```

Interactive docs: `/docs`.

## `POST /search`

```bash
curl -s -X POST "$BASE/search" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"query":"who won the nobel prize in physics"}'
```

```json
[{"docid": "29969", "score": 1.0, "snippet": "---\ntitle: ...\n---\n..."}]
```

10 results, descending score. Snippets are capped at 512 tokens.

## `POST /retrieve`

Same request and ranking, Search-R1 response shape — title split out, no score:

```json
{"result": [{"docid": "64251", "document": {"title": "...", "text": "..."}}]}
```

## `GET /get_document`

Full untruncated text by docid. No GPU work, responds in milliseconds; `404` if unknown.

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/get_document?docid=75737"
```

```json
{"docid": "75737", "text": "---\ntitle: List of Nobel laureates in Physics ...\n---\n..."}
```

## Python

```python
import os, requests

BASE = os.environ["BCP_RETRIEVAL_URL"]   # http://$PUBLIC_IPADDR:$VAST_TCP_PORT_10100
S = requests.Session()
S.headers["Authorization"] = f"Bearer {os.environ['TOKEN']}"

hits = S.post(f"{BASE}/search", json={"query": "..."}, timeout=60).json()
full = S.get(f"{BASE}/get_document", params={"docid": hits[0]["docid"]}).json()["text"]
```

## Performance

Measured on the original serving box (1x RTX 4090, flash-attn + rerank token cache
live), `scripts_evaluation/bench_concurrency.py` — re-run it after a rebuild rather
than assuming these carry over:

| concurrent callers | per-caller latency | throughput |
|--------------------|--------------------|------------|
| 1                  | **4.5 s**          | 0.22 q/s   |
| 3                  | 4.4 / 8.3 / **12.6 s** | 0.24 q/s |

A single search spends ~90% of its time in the cross-encoder (stage 1 ~1.5 s over
~200 candidates, stage 2 ~2.5 s over the surviving 20). The rest is the dense scan
(~150 ms), the 8B query encoder (~60 ms), and BM25 (~200 ms).

## Operational notes

- **Client timeout: 60 s.** Typical responses are 4-5 s alone and up to ~13 s with
  three callers in flight. 60 s leaves room for a cold first request without hanging
  a caller for minutes if something is wrong.
- **GPU work is still serialized.** One lock covers the query encoder and the
  reranker, so concurrent searches queue for the GPU: with N callers the last one
  waits roughly N x the solo latency. BM25 and the dense scan now run outside that
  lock and overlap across requests.
- **Modest parallelism is fine, and helps throughput** — 3 concurrent callers finish
  in 12.6 s wall versus 13.5 s issued serially. Beyond ~3 concurrent the queue grows
  faster than it drains; batch your own work rather than fanning out widely.
- **`/get_document` no longer queues behind searches** in the same way — it touches
  no GPU — but it still shares the HTTP worker pool.
- **`k` is fixed at 10** server-side — not a request parameter.
- Only `query` is accepted in the body; extra fields are ignored.
- After a restart the service needs **3-4 minutes** to load the corpus, indexes,
  models, and the rerank token cache before it binds the port.
  `supervisorctl status bcp-retrieval` reporting `RUNNING` does not mean it is
  accepting connections — poll `/docs` instead.
- Per-request stage timings are logged to `/var/log/portal/bcp-retrieval.log`
  (`search timing: ... rerank=... total=...`), which is the fastest way to tell a
  slow query from a queued one.

## Local access (External Endpoint cant access this)

From inside the container, skip Caddy and the token entirely:

```bash
curl -s -X POST http://127.0.0.1:17070/search \
  -H 'Content-Type: application/json' -d '{"query":"..."}'
```

For a private remote session, forward the internal port over SSH rather than using the
public one:

```bash
ssh -p $VAST_TCP_PORT_22 -L 8080:127.0.0.1:17070 root@$PUBLIC_IPADDR
```
