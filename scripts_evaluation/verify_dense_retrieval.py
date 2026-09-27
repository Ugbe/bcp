#!/usr/bin/env python
"""Recall gate for a dense retrieval service, measured over HTTP.

Sends every benchmark question to POST /search and reports evidence and gold recall@k
against the qrels, next to the ceiling min(k, |qrels|) / |qrels| that no ranking can
exceed. Recall is averaged over every qid in the qrels, counting a failed or missing
query as 0, which matches pyserini's `trec_eval -c`.

Reference for the Atom Neutron 0.6B service (neutron-retrieval/README.md), k=5:

  neutron                    evidence 0.8105 (ceiling 0.8128), gold 0.9497 (ceiling 0.9683)
  Qwen/Qwen3-Embedding-0.6B  evidence ~0.065

The overfit encoder was trained on these exact queries, so this checks that the service
reproduces the checkpoint, not that it generalizes.
"""

import argparse
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]


def read_qrels(path: Path) -> dict:
    qrels = defaultdict(set)
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 4 and int(parts[3]) > 0:
            qrels[parts[0]].add(parts[2])
    return dict(qrels)


def read_queries(path: Path) -> list:
    queries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[1].strip():
            queries.append((parts[0].strip(), parts[1].strip()))
    return queries


def mean_recall(retrieved: dict, qrels: dict, k: int) -> tuple:
    recall = ceiling = 0.0
    for qid, relevant in qrels.items():
        hits = set(retrieved.get(qid, [])[:k])
        recall += len(hits & relevant) / len(relevant)
        ceiling += min(k, len(relevant)) / len(relevant)
    return recall / len(qrels), ceiling / len(qrels)


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("BCP_RETRIEVAL_URL"))
    ap.add_argument("--token", default=os.environ.get("BCP_TOKEN"))
    ap.add_argument("--model", default=os.environ.get("BCP_RETRIEVAL_MODEL") or None)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--queries", type=Path, default=REPO_ROOT / "topics-qrels/queries.tsv")
    ap.add_argument("--qrels-evidence", type=Path, default=REPO_ROOT / "topics-qrels/qrel_evidence.txt")
    ap.add_argument("--qrels-gold", type=Path, default=REPO_ROOT / "topics-qrels/qrel_golds.txt")
    ap.add_argument("--limit", type=int, default=0, help="only the first N queries (recall still averages over all qrels)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--min-evidence", type=float, default=None, help="exit 1 if evidence recall@k is below this")
    ap.add_argument("--min-gold", type=float, default=None, help="exit 1 if gold recall@k is below this")
    args = ap.parse_args()
    if not args.url:
        ap.error("--url or BCP_RETRIEVAL_URL is required")

    queries = read_queries(args.queries)
    if args.limit:
        queries = queries[: args.limit]
    evidence = read_qrels(args.qrels_evidence)
    gold = read_qrels(args.qrels_gold)

    session = requests.Session()
    if args.token:
        session.headers["Authorization"] = f"Bearer {args.token}"
    base = args.url.rstrip("/")

    def search(item):
        qid, text = item
        body = {"query": text, "k": args.k, "include_text": False}
        if args.model:
            body["model"] = args.model
        try:
            r = session.post(f"{base}/search", json=body, timeout=args.timeout)
            r.raise_for_status()
            return qid, [str(h["docid"]) for h in r.json()["results"]], None
        except Exception as e:  # noqa: BLE001 - counted as a failed query
            return qid, [], f"{type(e).__name__}: {e}"

    retrieved, errors = {}, []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for n, (qid, docids, error) in enumerate(pool.map(search, queries), 1):
            retrieved[qid] = docids
            if error:
                errors.append((qid, error))
            if n % 100 == 0:
                print(f"  {n}/{len(queries)} queries", flush=True)

    short = [qid for qid, docids in retrieved.items() if len(docids) != args.k and qid not in dict(errors)]
    ev, ev_ceiling = mean_recall(retrieved, evidence, args.k)
    go, go_ceiling = mean_recall(retrieved, gold, args.k)
    print(f"service {base}  model {args.model or '(server default)'}  k={args.k}  queries {len(queries)}")
    print(f"evidence recall@{args.k}: {ev:.4f}  (ceiling {ev_ceiling:.4f}, {len(evidence)} qids)")
    print(f"gold     recall@{args.k}: {go:.4f}  (ceiling {go_ceiling:.4f}, {len(gold)} qids)")
    if errors:
        print(f"{len(errors)} failed queries, first: {errors[0]}")
    if short:
        print(f"{len(short)} queries returned a number of hits other than k={args.k}, e.g. qid {short[0]}")

    failed = bool(errors) or bool(short)
    if args.min_evidence is not None and ev < args.min_evidence:
        print(f"FAIL: evidence recall {ev:.4f} < {args.min_evidence}")
        failed = True
    if args.min_gold is not None and go < args.min_gold:
        print(f"FAIL: gold recall {go:.4f} < {args.min_gold}")
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
