"""Fetch every artifact the hybrid retrieval server needs.

Base models land in HF_HOME; indexes land under BrowseComp-Plus/indexes/.
Safe to re-run: hf_hub caches and skips completed files.
"""

import os
import sys
import time

from dotenv import load_dotenv
from huggingface_hub import snapshot_download

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEXES = os.path.join(REPO_ROOT, "indexes")

load_dotenv(os.path.join(REPO_ROOT, ".env"))
TOKEN = os.getenv("HF_TOKEN")

JOBS = [
    # (repo_id, repo_type, allow_patterns, local_dir)
    ("Qwen/Qwen3-Embedding-8B", "model", None, None),
    ("DanielTobi0/qwen3-embedding-8b-browsecomp-lora-v2", "model", None, None),
    ("Qwen/Qwen3-Reranker-0.6B", "model", None, None),
    ("DanielTobi0/qwen3-reranker-0.6b-browsecomp-lora-v3", "model", None, None),
    (
        "DanielTobi0/browsecomp-plus-qwen3-embedding-8b-finetuned-index-v2",
        "dataset",
        None,
        os.path.join(INDEXES, "qwen3-embedding-8b-finetuned-v2"),
    ),
    (
        "Tevatron/browsecomp-plus-indexes",
        "dataset",
        ["bm25/*"],
        INDEXES,
    ),
    ("Tevatron/browsecomp-plus-corpus", "dataset", None, None),
]


def main():
    for repo_id, repo_type, patterns, local_dir in JOBS:
        t0 = time.time()
        print(f"[downloading] {repo_type}: {repo_id}", flush=True)
        for attempt in range(1, 4):
            try:
                path = snapshot_download(
                    repo_id,
                    repo_type=repo_type,
                    allow_patterns=patterns,
                    local_dir=local_dir,
                    token=TOKEN,
                    max_workers=8,
                )
                print(
                    f"[done] {repo_id} -> {path} ({time.time() - t0:.0f}s)", flush=True
                )
                break
            except Exception as e:
                print(
                    f"[retry {attempt}/3] {repo_id}: {type(e).__name__}: {e}", flush=True
                )
                if attempt == 3:
                    print(f"[FAILED] {repo_id}", flush=True)
                    sys.exit(1)
                time.sleep(5 * attempt)

    print("[ALL DOWNLOADS COMPLETE]", flush=True)


if __name__ == "__main__":
    main()
