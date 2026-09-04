"""Compare candidate-pool configurations using a single rerank pass per query.

Reranker scores depend only on (query, document), not on which pool the document
came from, so one pass over the full BM25+dense union yields the final ranking for
every candidate-set variant. That makes an otherwise expensive sweep cheap.

    python scripts_evaluation/tune_pipeline.py --num-queries 30
"""

import argparse
import collections
import os
import random
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "searcher"))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts_evaluation"))

from eval_retrieval import load_qrels, load_queries, mean, recall  # noqa: E402
from searchers import HybridSearcher  # noqa: E402


def build(cli):
    args = argparse.Namespace(
        index_path=os.path.join(
            REPO_ROOT, "indexes/qwen3-embedding-8b-finetuned-v2/corpus.shard*.pkl"
        ),
        bm25_index_path=os.path.join(REPO_ROOT, "indexes/bm25"),
        embedding_model="Qwen/Qwen3-Embedding-8B",
        embedding_lora="DanielTobi0/qwen3-embedding-8b-browsecomp-lora-v2",
        reranker_model="Qwen/Qwen3-Reranker-0.6B",
        reranker_lora="DanielTobi0/qwen3-reranker-0.6b-browsecomp-lora-v3",
        dataset_name="Tevatron/browsecomp-plus-corpus",
        candidates_k=cli.candidates_k,
        rrf_k=60,
        rerank_depth=10_000,
        rerank_token_budget=cli.rerank_token_budget,
        rerank_max_batch=16,
        rerank_max_length=8192,
        query_max_length=4096,
        task_prefix=(
            "Instruct: Given a web search query, retrieve relevant passages that "
            "answer the query\nQuery:"
        ),
        attn_implementation="sdpa",
        disable_rerank=False,
        disable_bm25=False,
        disable_dense=False,
    )
    return args, HybridSearcher(args)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--num-queries", type=int, default=30)
    p.add_argument("--candidates-k", type=int, default=100)
    p.add_argument("--rerank-token-budget", type=int, default=16384)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument(
        "--qrels", default=os.path.join(REPO_ROOT, "topics-qrels/qrel_golds.txt")
    )
    cli = p.parse_args()

    queries = load_queries(os.path.join(REPO_ROOT, "data/queries.tsv"))
    qrels = load_qrels(cli.qrels)
    qids = sorted(set(queries) & set(qrels))
    random.Random(cli.seed).shuffle(qids)
    qids = qids[: cli.num_queries]

    args, s = build(cli)
    stats = collections.defaultdict(list)

    for i, qid in enumerate(qids, 1):
        query, gold = queries[qid], qrels[qid]

        bm25 = s._bm25_search(query, cli.candidates_k)
        dense = s._dense_search(query, cli.candidates_k)
        fused = [d for d, _ in s._rrf([bm25, dense], args.rrf_k)]

        # One rerank pass over the whole union; every variant below is a filter on it.
        scores = dict(s._rerank(query, fused))

        def reranked(pool):
            return sorted(pool, key=lambda d: scores.get(d, -1e9), reverse=True)

        # no-rerank baselines
        stats["dense@10 (no rerank)"].append(recall(dense, gold, 10))
        stats["fused@10 (no rerank)"].append(recall(fused, gold, 10))
        # rerank over varying candidate pools
        stats["rerank(dense50)@10"].append(recall(reranked(dense[:50]), gold, 10))
        stats["rerank(dense100)@10"].append(recall(reranked(dense[:100]), gold, 10))
        stats["rerank(fused100)@10"].append(recall(reranked(fused[:100]), gold, 10))
        stats["rerank(fused_all)@10"].append(recall(reranked(fused), gold, 10))
        # ceilings
        stats["ceiling dense100"].append(recall(dense, gold, 100))
        stats["ceiling fused_all"].append(recall(fused, gold, len(fused)))

        print(f"[{i}/{len(qids)}] qid={qid} pool={len(fused)}", flush=True)

    print("\n===== mean recall@10 over %d queries =====" % len(qids))
    for key in [
        "dense@10 (no rerank)",
        "fused@10 (no rerank)",
        "rerank(dense50)@10",
        "rerank(dense100)@10",
        "rerank(fused100)@10",
        "rerank(fused_all)@10",
        "ceiling dense100",
        "ceiling fused_all",
    ]:
        print(f"{key:26} {mean(stats[key]):.4f}")


if __name__ == "__main__":
    main()
