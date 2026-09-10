"""Audit where qrel evidence enters the retrieval pool.

This is the offline diagnostic requested by roadmap section 4.4.4.  It does
not change benchmark records.  For each selected question it runs the full
question plus optional hand-written constraint rewrites, records the first
rank of every qrel evidence document, and explicitly reports when the remote
API only returned its legacy top ten instead of the requested pool.

Constraint file formats accepted:
  JSON object: {"qid": ["constraint query", ...]}
  TSV: qid<TAB>constraint query (repeat qid for multiple constraints)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from searcher.searchers.remote_api_searcher import RemoteApiSearcher

DEFAULT_QUERIES = ROOT / "topics-qrels" / "queries.tsv"
DEFAULT_QRELS = ROOT / "topics-qrels" / "qrel_evidence.txt"
DEFAULT_METRICS = ROOT / "analysis" / "atom-electron-1.3-9b-830-audit" / "per_run_metrics.csv"
DEFAULT_OUTPUT = ROOT / "analysis" / "zero-recall-retrieval-audit.json"
load_dotenv(ROOT / ".env")


def load_queries(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8", newline="") as handle:
        return {
            row[0].strip(): row[1].strip()
            for row in csv.reader(handle, delimiter="\t")
            if len(row) >= 2 and row[0].strip()
        }


def load_qrels(path: Path) -> dict[str, set[str]]:
    evidence: dict[str, set[str]] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.reader(handle, delimiter=" "):
            fields = [item for item in row if item]
            if len(fields) >= 3:
                evidence.setdefault(fields[0], set()).add(str(fields[2]))
    return evidence


def load_constraints(path: Path | None) -> dict[str, list[str]]:
    if path is None:
        return {}
    if path.suffix.lower() == ".json":
        value = json.loads(path.read_text(encoding="utf-8"))
        return {
            str(qid): [str(item) for item in queries if str(item).strip()]
            for qid, queries in value.items()
            if isinstance(queries, list)
        }
    constraints: dict[str, list[str]] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.reader(handle, delimiter="\t"):
            if len(row) >= 2 and row[0].strip() and row[1].strip():
                constraints.setdefault(row[0].strip(), []).append(row[1].strip())
    return constraints


def select_zero_recall(metrics_path: Path | None, limit: int | None) -> set[str] | None:
    if metrics_path is None:
        return None
    selected = []
    with metrics_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                recall = float(row.get("retrieval_recall") or 0)
            except ValueError:
                continue
            if recall == 0:
                selected.append(str(row["query_id"]))
    return set(selected[:limit] if limit else selected)


def audit_query(searcher: RemoteApiSearcher, qid: str, question: str, evidence: set[str], constraints: list[str], pool_k: int) -> dict:
    queries = [("full_question", question)] + [
        (f"constraint_{index}", query) for index, query in enumerate(constraints, start=1)
    ]
    attempts = []
    first_rank: dict[str, dict] = {docid: {"rank": None, "query_type": None, "query": None} for docid in evidence}
    for query_type, query in queries:
        response = searcher.search_pool(query, pool_k)
        rows = response.get("results", [])
        ranks = {str(item.get("docid")): rank for rank, item in enumerate(rows, start=1)}
        for docid in evidence:
            if first_rank[docid]["rank"] is None and docid in ranks:
                first_rank[docid] = {"rank": ranks[docid], "query_type": query_type, "query": query}
        attempts.append(
            {
                "query_type": query_type,
                "query": query,
                "returned_count": len(rows),
                "metadata": response.get("metadata") or {},
                "evidence_docids_found": sorted(set(ranks) & evidence),
            }
        )
    found = [item["rank"] for item in first_rank.values() if item["rank"] is not None]
    return {
        "query_id": qid,
        "question": question,
        "evidence_docids": sorted(evidence),
        "first_rank_by_docid": first_rank,
        "best_rank": min(found) if found else None,
        "evidence_found": bool(found),
        "retriever_ceiling_within_requested_pool": not bool(found),
        "attempts": attempts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit zero-recall BrowseComp questions against deep retrieval pools.")
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    parser.add_argument("--qrels", type=Path, default=DEFAULT_QRELS)
    parser.add_argument("--metrics", type=Path, default=DEFAULT_METRICS)
    parser.add_argument("--constraints", type=Path, default=None)
    parser.add_argument("--qids", nargs="*", help="Explicit qids; otherwise use zero-recall rows from --metrics")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--pool-k", type=int, default=1000)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--retrieval-url", default=os.environ.get("BCP_RETRIEVAL_URL", "http://127.0.0.1:17070"))
    parser.add_argument("--retrieval-token", default=os.environ.get("BCP_TOKEN"))
    parser.add_argument("--retrieval-timeout", type=float, default=60.0)
    parser.add_argument("--retrieval-retries", type=int, default=3)
    parser.add_argument("--retrieval-retry-backoff", type=float, default=5.0)
    args = parser.parse_args()

    queries = load_queries(args.queries)
    qrels = load_qrels(args.qrels)
    constraints = load_constraints(args.constraints)
    selected = set(args.qids) if args.qids else select_zero_recall(args.metrics, args.limit)
    if selected is None:
        selected = set(list(queries)[: args.limit])
    selected = [qid for qid in queries if qid in selected and qid in qrels]
    remote_args = SimpleNamespace(
        retrieval_url=args.retrieval_url,
        retrieval_token=args.retrieval_token,
        retrieval_timeout=args.retrieval_timeout,
        retrieval_retries=args.retrieval_retries,
        retrieval_retry_backoff=args.retrieval_retry_backoff,
    )
    searcher = RemoteApiSearcher(remote_args)
    records = [
        audit_query(searcher, qid, queries[qid], qrels[qid], constraints.get(qid, []), args.pool_k)
        for qid in selected
    ]
    supported_attempts = [
        attempt
        for record in records
        for attempt in record["attempts"]
        if (attempt.get("metadata") or {}).get("pool_supported")
    ]
    found = sum(record["evidence_found"] for record in records)
    output = {
        "schema_version": 1,
        "requested_pool_k": args.pool_k,
        "question_count": len(records),
        "questions_with_evidence_in_pool": found,
        "questions_without_evidence_in_pool": len(records) - found,
        "deep_pool_supported_attempts": len(supported_attempts),
        "legacy_top_ten_warning": any(
            not (attempt.get("metadata") or {}).get("pool_supported", False)
            for record in records for attempt in record["attempts"]
        ),
        "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({key: output[key] for key in output if key != "records"}, indent=2))
    print(f"Wrote detailed audit to {args.output}")


if __name__ == "__main__":
    main()
