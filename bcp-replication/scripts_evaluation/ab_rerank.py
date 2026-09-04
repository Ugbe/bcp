"""A/B the rerank strategy: quality and latency, on identical candidate pools.

Loads the searcher once (the 8B encoder takes minutes) and runs every variant over
the same BM25+dense+RRF fusion, so any difference in recall comes from the rerank
strategy alone and not from a different candidate pool or a reshuffled sample.

    python scripts_evaluation/ab_rerank.py --num-queries 25

The baseline variant is what production served before this change: every candidate
scored at 8192 tokens, single stage.
"""

import argparse
import collections
import contextlib
import json
import os
import random
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "searcher"))

from searchers import HybridSearcher  # noqa: E402

from eval_retrieval import load_qrels, load_queries, mean, recall  # noqa: E402


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
        rerank_max_length=8192,
        rerank_token_cache=cli.rerank_token_cache,
        rerank_stage1_length=512,
        rerank_stage1_keep=35,
        disable_cascade=False,
        query_max_length=4096,
        task_prefix=(
            "Instruct: Given a web search query, retrieve relevant passages that "
            "answer the query\nQuery:"
        ),
        attn_implementation=cli.attn_implementation,
        disable_rerank=False,
        disable_bm25=False,
        disable_dense=False,
    )
    return args, HybridSearcher(args)


# name -> attributes overlaid on searcher.args for that run
VARIANTS = [
    ("baseline-8192x200", dict(disable_cascade=True, rerank_max_length=8192)),
    ("cascade-512->8192x35", dict(disable_cascade=False, rerank_stage1_length=512, rerank_stage1_keep=35)),
    ("cascade-512->8192x20", dict(disable_cascade=False, rerank_stage1_length=512, rerank_stage1_keep=20)),
    ("cascade-1024->8192x35", dict(disable_cascade=False, rerank_stage1_length=1024, rerank_stage1_keep=35)),
    ("truncate-2048x200", dict(disable_cascade=True, rerank_max_length=2048)),
]


@contextlib.contextmanager
def count_tokens(searcher, counter):
    """Accumulate padded tokens actually pushed through the cross-encoder."""
    original = searcher._score_batch

    def wrapped(batch):
        longest = max(len(ids) for _, ids in batch)
        counter[0] += longest * len(batch)
        return original(batch)

    searcher._score_batch = wrapped
    try:
        yield
    finally:
        searcher._score_batch = original


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--num-queries", type=int, default=25)
    p.add_argument("--candidates-k", type=int, default=100)
    p.add_argument("--rerank-depth", type=int, default=200)
    p.add_argument("--rerank-token-budget", type=int, default=16384)
    p.add_argument("--rerank-max-batch", type=int, default=64)
    p.add_argument("--attn-implementation", default="flash_attention_2")
    p.add_argument(
        "--rerank-token-cache",
        default=os.path.join(REPO_ROOT, "indexes/rerank-token-cache"),
    )
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--only", default="", help="Comma-separated variant names to run.")
    p.add_argument("--out", default=os.path.join(REPO_ROOT, "ab_rerank_results.json"))
    p.add_argument(
        "--index-path",
        default=os.path.join(
            REPO_ROOT, "indexes/qwen3-embedding-8b-finetuned-v2/corpus.shard*.pkl"
        ),
    )
    p.add_argument("--bm25-index-path", default=os.path.join(REPO_ROOT, "indexes/bm25"))
    p.add_argument("--queries", default=os.path.join(REPO_ROOT, "data/queries.tsv"))
    p.add_argument(
        "--qrels", default=os.path.join(REPO_ROOT, "topics-qrels/qrel_golds.txt")
    )
    cli = p.parse_args()

    variants = VARIANTS
    if cli.only:
        wanted = {name.strip() for name in cli.only.split(",")}
        variants = [v for v in VARIANTS if v[0] in wanted]

    queries, qrels = load_queries(cli.queries), load_qrels(cli.qrels)
    qids = sorted(set(queries) & set(qrels))
    random.Random(cli.seed).shuffle(qids)
    qids = qids[: cli.num_queries]
    print(f"{len(qids)} queries, {len(variants)} variants", flush=True)

    args, searcher = build_searcher(cli)

    results = collections.defaultdict(lambda: collections.defaultdict(list))
    pool_recall = []

    for i, qid in enumerate(qids, 1):
        query, gold = queries[qid], qrels[qid]

        # One fusion, shared by every variant.
        bm25 = searcher._bm25_search(query, cli.candidates_k)
        dense = searcher._dense_search(query, cli.candidates_k)
        fused = [d for d, _ in searcher._rrf([bm25, dense], args.rrf_k)]
        candidates = fused[: cli.rerank_depth]
        pool_recall.append(recall(candidates, gold, len(candidates)))

        line = [f"[{i}/{len(qids)}] {qid} gold={len(gold)}"]
        for name, overrides in variants:
            for key, value in overrides.items():
                setattr(searcher.args, key, value)

            counter = [0]
            with count_tokens(searcher, counter):
                t0 = time.time()
                ranked = [d for d, _ in searcher._rerank(query, candidates)]
                elapsed = time.time() - t0

            results[name]["recall@10"].append(recall(ranked, gold, 10))
            results[name]["recall@5"].append(recall(ranked, gold, 5))
            results[name]["latency"].append(elapsed)
            results[name]["tokens"].append(counter[0])
            line.append(f"{name}={results[name]['recall@10'][-1]:.2f}/{elapsed:.1f}s")
        print("  ".join(line), flush=True)

    print(f"\ncandidate pool recall@{cli.rerank_depth}: {mean(pool_recall):.4f}  (ceiling)")
    header = f"{'variant':24} {'recall@10':>10} {'recall@5':>9} {'latency':>9} {'tokens':>10} {'speedup':>8}"
    print("\n" + header)
    print("-" * len(header))

    base_latency = None
    summary = {}
    for name, _ in variants:
        lat = mean(results[name]["latency"])
        if base_latency is None:
            base_latency = lat
        summary[name] = {
            "recall@10": mean(results[name]["recall@10"]),
            "recall@5": mean(results[name]["recall@5"]),
            "latency": lat,
            "tokens": mean(results[name]["tokens"]),
            "speedup": base_latency / lat if lat else 0.0,
        }
        s = summary[name]
        print(
            f"{name:24} {s['recall@10']:10.4f} {s['recall@5']:9.4f} "
            f"{s['latency']:8.1f}s {s['tokens']:10.0f} {s['speedup']:7.2f}x"
        )

    # Variant means on a small sample order themselves partly by luck. Everything
    # ran on the same queries and the same candidate pools, so compare per query and
    # bootstrap the paired difference instead of eyeballing the means.
    base_name = variants[0][0]
    base = results[base_name]["recall@10"]
    if len(variants) > 1:
        print(f"\npaired delta in recall@10 vs {base_name} (95% bootstrap CI):")
        rng = random.Random(cli.seed)
        n = len(base)
        idx_samples = [[rng.randrange(n) for _ in range(n)] for _ in range(5000)]
        for name, _ in variants[1:]:
            deltas = [a - b for a, b in zip(results[name]["recall@10"], base)]
            point = sum(deltas) / n
            boots = sorted(
                sum(deltas[i] for i in sample) / n for sample in idx_samples
            )
            lo, hi = boots[125], boots[4875]
            wins = sum(d > 0 for d in deltas)
            losses = sum(d < 0 for d in deltas)
            verdict = "no significant difference" if lo <= 0 <= hi else "SIGNIFICANT"
            print(
                f"  {name:24} {point:+.4f}  [{lo:+.4f}, {hi:+.4f}]  "
                f"better/worse/tied {wins}/{losses}/{n - wins - losses}  {verdict}"
            )
            summary[name]["delta_recall@10"] = point
            summary[name]["delta_ci95"] = [lo, hi]

    with open(cli.out, "w") as fh:
        json.dump(
            {
                "qids": qids,
                "pool_recall": mean(pool_recall),
                "variants": summary,
                "per_query": {
                    name: dict(results[name]) for name, _ in variants
                },
            },
            fh,
            indent=2,
        )
    print(f"\nwrote {cli.out}")


if __name__ == "__main__":
    main()
