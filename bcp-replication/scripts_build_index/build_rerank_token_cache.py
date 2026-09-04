"""Pre-tokenize the corpus for the cross-encoder, once, offline.

At request time the reranker spent ~2.2s per query tokenizing 200 documents in a
single-threaded Python loop -- re-doing identical work on every query while 255
cores sat idle. The document half of the rerank prompt does not depend on the
query (the query is interpolated *before* the document, see hybrid_searcher's
RERANK_BODY_TMPL), so it can be tokenized once and reused forever.

Output (indexes/rerank-token-cache/):
    tokens.i32   flat concatenation of every document's token ids
    offsets.npy  offsets[i]:offsets[i+1] slices document i out of tokens.i32
    docids.json  row index -> docid, and the build config used

Each document is stored as the token ids of "\\n<Document>: {text}" truncated to
--max-doc-tokens. Callers slice the prefix they can afford; nothing is ever
re-tokenized at query time.

    python scripts_build_index/build_rerank_token_cache.py
"""

import argparse
import json
import os
import time

import numpy as np
from datasets import load_dataset
from transformers import AutoTokenizer

# Must match hybrid_searcher.RERANK_DOC_PREFIX.
DOC_PREFIX = "\n<Document>: "

_TOK = None
_DS = None


def _init(tokenizer_name, dataset_name):
    global _TOK, _DS
    # Each worker tokenizes single documents; the Rust tokenizer's own thread pool
    # would oversubscribe 64 processes x N threads.
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    _TOK = AutoTokenizer.from_pretrained(tokenizer_name)
    _DS = load_dataset(dataset_name, split="train")


def _encode_range(job):
    start, end, max_tokens, char_budget = job
    out = []
    for i in range(start, end):
        text = _DS[i]["text"]
        # Only the first max_tokens survive truncation, so cut the raw string first:
        # some documents are >100k tokens and tokenizing them in full to discard the
        # tail is what made this slow. 8 chars/token overestimates the real ratio.
        ids = _TOK(
            DOC_PREFIX + text[:char_budget],
            truncation="longest_first",
            max_length=max_tokens,
            add_special_tokens=False,
        )["input_ids"]
        out.append(np.asarray(ids, dtype=np.int32))
    return start, out


def verify(tokenizer, dataset, out_dir, n=200, seed=0):
    """Check that pre-tokenizing the document half is bit-exact.

    Splitting one tokenization into two is only safe if no BPE merge spans the
    join. DOC_PREFIX starts with a newline, which Qwen's pretokenizer regex always
    splits on, so it should hold -- but assert it rather than assume it.
    """
    tokens = np.memmap(os.path.join(out_dir, "tokens.i32"), dtype=np.int32, mode="r")
    offsets = np.load(os.path.join(out_dir, "offsets.npy"))
    meta = json.load(open(os.path.join(out_dir, "docids.json")))
    max_tokens, char_budget = meta["max_doc_tokens"], meta["char_budget"]

    rng = np.random.default_rng(seed)
    queries = [
        "who won the nobel prize in physics",
        "which city hosted the 1964 summer olympics and what was its population",
        "a" * 400,  # long query, stresses the budget arithmetic
        "",
    ]
    bad = 0
    for i in rng.choice(len(dataset), n, replace=False):
        i = int(i)
        cached = np.asarray(tokens[offsets[i] : offsets[i + 1]])
        fresh = tokenizer(
            DOC_PREFIX + dataset[i]["text"][:char_budget],
            truncation="longest_first",
            max_length=max_tokens,
            add_special_tokens=False,
        )["input_ids"]
        if cached.tolist() != fresh:
            bad += 1
            continue
        # And that head + cached == tokenizing the whole body in one go.
        for q in queries[:2]:
            head = f"<Instruct>: task\n<Query>: {q}"
            joined = tokenizer(
                head + DOC_PREFIX + dataset[i]["text"][:char_budget],
                truncation="longest_first",
                max_length=max_tokens,
                add_special_tokens=False,
            )["input_ids"]
            head_ids = tokenizer(head, add_special_tokens=False)["input_ids"]
            recomposed = (head_ids + cached.tolist())[: max_tokens]
            if joined != recomposed[: len(joined)]:
                bad += 1
                break
    print(f"verification: {n - bad}/{n} documents bit-exact")
    return bad == 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tokenizer", default="DanielTobi0/qwen3-reranker-0.6b-browsecomp-lora-v3")
    p.add_argument("--dataset-name", default="Tevatron/browsecomp-plus-corpus")
    p.add_argument(
        "--out-dir",
        default=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "indexes/rerank-token-cache",
        ),
    )
    p.add_argument(
        "--max-doc-tokens",
        type=int,
        default=8192,
        help="Cap per document. Must be >= the largest rerank-max-length you will serve.",
    )
    p.add_argument("--workers", type=int, default=min(64, os.cpu_count() or 8))
    p.add_argument("--chunk", type=int, default=250)
    p.add_argument("--verify-only", action="store_true")
    cli = p.parse_args()

    os.makedirs(cli.out_dir, exist_ok=True)
    tokenizer = AutoTokenizer.from_pretrained(cli.tokenizer)
    ds = load_dataset(cli.dataset_name, split="train")
    n_docs = len(ds)
    char_budget = cli.max_doc_tokens * 8
    print(f"{n_docs} documents, cap {cli.max_doc_tokens} tokens, {cli.workers} workers")

    if cli.verify_only:
        ok = verify(tokenizer, ds, cli.out_dir)
        raise SystemExit(0 if ok else 1)

    import multiprocessing as mp

    jobs = [
        (s, min(s + cli.chunk, n_docs), cli.max_doc_tokens, char_budget)
        for s in range(0, n_docs, cli.chunk)
    ]
    results = [None] * n_docs
    t0 = time.time()
    done = 0
    ctx = mp.get_context("fork")
    with ctx.Pool(
        cli.workers, initializer=_init, initargs=(cli.tokenizer, cli.dataset_name)
    ) as pool:
        for start, encoded in pool.imap_unordered(_encode_range, jobs):
            results[start : start + len(encoded)] = encoded
            done += len(encoded)
            if done % 10000 < cli.chunk:
                rate = done / (time.time() - t0)
                print(
                    f"  {done}/{n_docs}  {rate:.0f} docs/s  "
                    f"eta {(n_docs - done) / rate:.0f}s",
                    flush=True,
                )

    lengths = np.array([len(r) for r in results], dtype=np.int64)
    offsets = np.zeros(n_docs + 1, dtype=np.int64)
    np.cumsum(lengths, out=offsets[1:])
    total = int(offsets[-1])

    print(f"packing {total} tokens ({total * 4 / 1e9:.2f} GB)")
    tokens = np.memmap(
        os.path.join(cli.out_dir, "tokens.i32"), dtype=np.int32, mode="w+", shape=(total,)
    )
    for i, arr in enumerate(results):
        tokens[offsets[i] : offsets[i + 1]] = arr
    tokens.flush()
    del tokens

    np.save(os.path.join(cli.out_dir, "offsets.npy"), offsets)
    with open(os.path.join(cli.out_dir, "docids.json"), "w") as fh:
        json.dump(
            {
                "docids": list(ds["docid"]),
                "tokenizer": cli.tokenizer,
                "dataset_name": cli.dataset_name,
                "max_doc_tokens": cli.max_doc_tokens,
                "char_budget": char_budget,
                "doc_prefix": DOC_PREFIX,
                "n_tokens": total,
            },
            fh,
        )

    print(
        f"built in {time.time() - t0:.0f}s: mean {lengths.mean():.0f} tok/doc, "
        f"median {np.median(lengths):.0f}, {(lengths >= cli.max_doc_tokens).mean():.1%} at cap"
    )
    verify(tokenizer, ds, cli.out_dir)


if __name__ == "__main__":
    main()
