"""Measure retrieval quality of the hybrid pipeline, stage by stage.

Loads the searcher once and reports recall for each leg (BM25, dense), the RRF
fusion, and the reranked output. Its main job is catching a query-encoder that
does not match the dense index: a mismatch shows up as near-zero dense recall
while BM25 recall stays normal.

    python scripts_evaluation/eval_retrieval.py --num-queries 30
"""

import argparse
import collections
import os
import random
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "searcher"))

from searchers import HybridSearcher  # noqa: E402


def load_qrels(path):
    qrels = collections.defaultdict(set)
    with open(path) as fh:
        for line in fh:
            parts = line.split()
            if len(parts) >= 4 and int(parts[3]) > 0:
                qrels[parts[0]].add(parts[2])
    return qrels


def load_queries(path):
    queries = {}
    with open(path) as fh:
        for line in fh:
            qid, _, text = line.rstrip("\n").partition("\t")
            if text:
                queries[qid] = text
    return queries


def recall(retrieved, relevant, k):
    if not relevant:
        return None
    hits = len(set(retrieved[:k]) & relevant)
    return hits / len(relevant)


def mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else 0.0


def build_searcher(cli):
    args = argparse.Namespace(
        index_path=cli.index_path,
        bm25_index_path=cli.bm25_index_path,
        embedding_model="Qwen/Qwen3-Embedding-8B",
        embedding_lora="DanielTobi0/qwen3-embedding-8b-browsecomp-lora-v2",
        reranker_model="Qwen/Qwen3-Reranker-0.6B",
        reranker_lora="DanielTobi0/qwen3-reranker-0.6b-browsecomp-lora-v3",
        dataset_name="Tevatron/browsecomp-plus-corpus",
        candidates_k=cli.candidates_k,
        rrf_k=60,
        rerank_depth=cli.rerank_depth,
        rerank_token_budget=cli.rerank_token_budget,
        rerank_max_batch=cli.rerank_max_batch,
        rerank_max_length=cli.rerank_max_length,
        rerank_token_cache=cli.rerank_token_cache,
        rerank_stage1_length=cli.rerank_stage1_length,
        rerank_stage1_keep=cli.rerank_stage1_keep,
        disable_cascade=cli.disable_cascade,
        query_max_length=4096,
        task_prefix=(
            "Instruct: Given a web search query, retrieve relevant passages that "
            "answer the query\nQuery:"
        ),
        attn_implementation=cli.attn_implementation,
        disable_rerank=cli.disable_rerank,
        disable_bm25=False,
        disable_dense=False,
    )
    return args, HybridSearcher(args)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--num-queries", type=int, default=30)
    p.add_argument("--candidates-k", type=int, default=100)
    p.add_argument("--rerank-depth", type=int, default=100)
    p.add_argument("--rerank-token-budget", type=int, default=16384)
    p.add_argument("--rerank-max-batch", type=int, default=16)
    p.add_argument("--rerank-max-length", type=int, default=8192)
    p.add_argument("--rerank-token-cache", default=None)
    p.add_argument("--rerank-stage1-length", type=int, default=512)
    p.add_argument("--rerank-stage1-keep", type=int, default=35)
    p.add_argument("--disable-cascade", action="store_true")
    p.add_argument("--attn-implementation", default="sdpa")
    p.add_argument("--disable-rerank", action="store_true")
    p.add_argument(
        "--tag", default="", help="Label printed with the summary, for A/B runs."
    )
    p.add_argument("--seed", type=int, default=0)
    p.add_argument(
        "--index-path",
        default=os.path.join(
            REPO_ROOT, "indexes/qwen3-embedding-8b-finetuned-v2/corpus.shard*.pkl"
        ),
    )
    p.add_argument(
        "--bm25-index-path", default=os.path.join(REPO_ROOT, "indexes/bm25")
    )
    p.add_argument("--queries", default=os.path.join(REPO_ROOT, "data/queries.tsv"))
    p.add_argument(
        "--qrels", default=os.path.join(REPO_ROOT, "topics-qrels/qrel_golds.txt")
    )
    cli = p.parse_args()

    queries = load_queries(cli.queries)
    qrels = load_qrels(cli.qrels)

    qids = sorted(set(queries) & set(qrels))
    print(f"{len(queries)} queries, {len(qrels)} with qrels, {len(qids)} usable")
    if not qids:
        sys.exit("No overlap between queries and qrels -- check id formats.")

    random.Random(cli.seed).shuffle(qids)
    qids = qids[: cli.num_queries]

    args, searcher = build_searcher(cli)

    # docid namespace sanity check
    sample_gold = next(iter(qrels[qids[0]]))
    print(
        f"docid check: gold={sample_gold!r} in corpus={sample_gold in searcher.docid_to_text}, "
        f"in dense lookup={sample_gold in set(searcher.lookup[:1000]) or sample_gold in set(searcher.lookup)}"
    )

    stats = collections.defaultdict(list)
    latencies = []

    for i, qid in enumerate(qids, 1):
        query, gold = queries[qid], qrels[qid]

        bm25 = searcher._bm25_search(query, cli.candidates_k)
        dense = searcher._dense_search(query, cli.candidates_k)
        fused = [d for d, _ in searcher._rrf([bm25, dense], args.rrf_k)]

        stats["bm25@100"].append(recall(bm25, gold, cli.candidates_k))
        # dense@10 is the baseline the reranker has to beat to justify its latency.
        stats["dense@10"].append(recall(dense, gold, 10))
        stats["dense@100"].append(recall(dense, gold, cli.candidates_k))
        # What BM25 adds to the candidate pool that dense missed.
        stats["bm25_unique_gold"].append(
            len((set(bm25) & gold) - set(dense)) / len(gold) if gold else None
        )
        stats["fused@10"].append(recall(fused, gold, 10))
        stats["fused@100"].append(recall(fused, gold, 100))
        # Ceiling on the final answer: nothing outside the reranker's candidate
        # window can ever be returned, however good the reranker is.
        stats["pool(union)"].append(recall(fused, gold, len(fused)))
        stats[f"pool@depth{cli.rerank_depth}"].append(
            recall(fused, gold, cli.rerank_depth)
        )

        if not cli.disable_rerank:
            t0 = time.time()
            reranked = [
                d for d, _ in searcher._rerank(query, fused[: cli.rerank_depth])
            ]
            latencies.append(time.time() - t0)
            stats["reranked@10"].append(recall(reranked, gold, 10))
            stats["reranked@5"].append(recall(reranked, gold, 5))

        print(
            f"[{i}/{len(qids)}] qid={qid} gold={len(gold)} "
            + " ".join(f"{k}={stats[k][-1]:.2f}" for k in sorted(stats)),
            flush=True,
        )

    mode = (
        "single-stage"
        if cli.disable_cascade
        else f"cascade({cli.rerank_stage1_length}tok x{cli.rerank_depth} "
        f"-> {cli.rerank_max_length}tok x{cli.rerank_stage1_keep})"
    )
    print(f"\n===== mean recall  [{cli.tag or mode}] =====")
    for key in sorted(stats):
        print(f"{key:22} {mean(stats[key]):.4f}")
    if latencies:
        ordered = sorted(latencies)
        print(
            f"\nrerank latency: mean {sum(latencies) / len(latencies):.1f}s "
            f"p50 {ordered[len(ordered) // 2]:.1f}s max {max(ordered):.1f}s"
        )
    print(
        f"config: mode={mode} depth={cli.rerank_depth} budget={cli.rerank_token_budget} "
        f"max_batch={cli.rerank_max_batch} attn={cli.attn_implementation} "
        f"cache={'yes' if cli.rerank_token_cache else 'no'}"
    )


if __name__ == "__main__":
    main()
