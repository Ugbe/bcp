"""Dense retrieval service for BrowseComp-Plus with the Atom Neutron 0.6B encoder.

Serves the "dense" contract the runner speaks (searcher/searchers/remote_api_searcher.py
with BCP_RETRIEVAL_API=dense):

  POST /search          {"query", "k"?=10, "include_text"?=true, "model"?}
                        -> {"model", "k", "results": [{"docid", "score", "text"?}]}
  GET  /document/{id}   -> {"docid", "text"}            (404 if unknown)
  POST /documents       {"docids": [...]} -> {"documents": [{"docid", "text"}]}
  GET  /health, GET /info

Two query encoders share one base model and one document index:

  browsecomp-overfit          Qwen/Qwen3-Embedding-0.6B + CrowtherLabs/Atom-Neutron-emb-0.6b.
                              The adapter was trained on all 830 benchmark queries and their
                              qrels, so it is an oracle: an upper bound, never a held-out score.
  Qwen/Qwen3-Embedding-0.6B   The unmodified base model, a non-oracle control.

The adapter is query-side only; the document tower was frozen during training, so the
official prebuilt Tevatron index (encoded with the unmodified base) is the right index
for both encoders.

The encoding convention is load-bearing and must not be "fixed" (see README.md):
the query prefix is concatenated with no separator, the tokenizer's own special tokens
are used (no extra EOS appended), padding is on the left, the embedding is the last
token's hidden state, and it is L2-normalized. One query is encoded per request, with
no padding, exactly as the adapter's own agent runner did.
"""

import argparse
import glob
import logging
import os
import pickle
import threading
import time
from typing import Callable, Dict, List, Optional

import numpy as np
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger("neutron")

QUERY_PREFIX = (
    "Instruct: Given a web search query, retrieve relevant passages "
    "that answer the query\nQuery:"
)
BASE_MODEL = "Qwen/Qwen3-Embedding-0.6B"
ADAPTER_REPO = "CrowtherLabs/Atom-Neutron-emb-0.6b"
OVERFIT_NAME = "browsecomp-overfit"
INDEX_REPO = "Tevatron/browsecomp-plus-indexes"
INDEX_SUBDIR = "qwen3-embedding-0.6b"
CORPUS_REPO = "Tevatron/browsecomp-plus-corpus"
MAX_K = 1000
MAX_BATCH_DOCIDS = 100

# encode(query, encoder_name) -> L2-normalized float32 vector
EncodeFn = Callable[[str, str], np.ndarray]


class SearchRequest(BaseModel):
    query: str
    k: int = 10
    include_text: bool = True
    model: Optional[str] = None


class DocumentsRequest(BaseModel):
    docids: List[str]


class Retriever:
    """Exact inner-product search over an L2-normalized document matrix."""

    def __init__(
        self,
        reps: np.ndarray,
        lookup: List[str],
        docs: Dict[str, str],
        encode: EncodeFn,
        encoders: List[str],
        default_encoder: str,
    ):
        if reps.ndim != 2 or reps.shape[0] != len(lookup):
            raise ValueError(f"index shape {reps.shape} does not match {len(lookup)} docids")
        if default_encoder not in encoders:
            raise ValueError(f"default encoder {default_encoder!r} is not served")
        self.reps = np.ascontiguousarray(reps, dtype=np.float32)
        self.lookup = [str(docid) for docid in lookup]
        self.docs = docs
        self.encode = encode
        self.encoders = list(encoders)
        self.default_encoder = default_encoder

    def search(self, query: str, k: int, encoder: Optional[str]) -> List[Dict[str, object]]:
        name = encoder or self.default_encoder
        if name not in self.encoders:
            raise KeyError(name)
        k = max(1, min(int(k), MAX_K, len(self.lookup)))
        q = np.asarray(self.encode(query, name), dtype=np.float32)
        scores = self.reps @ q
        # argpartition picks arbitrarily among documents tied at the k-th score
        # (duplicate documents share a vector), so fill those slots by index.
        kth = scores[np.argpartition(-scores, k - 1)[:k]].min()
        above = np.flatnonzero(scores > kth)
        tied = np.flatnonzero(scores == kth)
        top = np.concatenate([above, tied[: k - len(above)]])
        # Stable order: score descending, then index position.
        top = top[np.lexsort((top, -scores[top]))]
        return [{"docid": self.lookup[i], "score": float(scores[i])} for i in top]


def build_app(retriever: Retriever, api_token: Optional[str] = None) -> FastAPI:
    app = FastAPI(title="Atom Neutron dense retrieval (BrowseComp-Plus)")

    def require_token(request: Request) -> None:
        if not api_token:
            return
        if request.headers.get("authorization") != f"Bearer {api_token}":
            raise HTTPException(401, "missing or wrong bearer token")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/info", dependencies=[Depends(require_token)])
    def info():
        return {
            "encoders": retriever.encoders,
            "default_encoder": retriever.default_encoder,
            "docs": len(retriever.lookup),
            "dim": int(retriever.reps.shape[1]),
            "oracle_warning": (
                f"{OVERFIT_NAME} was trained on all 830 BrowseComp-Plus queries and "
                "qrels; its results are an upper bound, not a held-out score."
            ),
        }

    @app.post("/search", dependencies=[Depends(require_token)])
    def search(req: SearchRequest):
        if not req.query.strip():
            raise HTTPException(400, "query must not be empty")
        started = time.perf_counter()
        try:
            hits = retriever.search(req.query, req.k, req.model)
        except KeyError:
            raise HTTPException(
                400, f"unknown model {req.model!r}; served: {retriever.encoders}"
            )
        if req.include_text:
            for hit in hits:
                hit["text"] = retriever.docs.get(hit["docid"], "")
        logger.info(
            "search model=%s k=%d %.0fms",
            req.model or retriever.default_encoder,
            len(hits),
            (time.perf_counter() - started) * 1000,
        )
        return {"model": req.model or retriever.default_encoder, "k": len(hits), "results": hits}

    @app.get("/document/{docid}", dependencies=[Depends(require_token)])
    def document(docid: str):
        if docid not in retriever.docs:
            raise HTTPException(404, f"docid {docid} not found")
        return {"docid": docid, "text": retriever.docs[docid]}

    @app.post("/documents", dependencies=[Depends(require_token)])
    def documents(req: DocumentsRequest):
        if len(req.docids) > MAX_BATCH_DOCIDS:
            raise HTTPException(400, f"at most {MAX_BATCH_DOCIDS} docids per request")
        return {
            "documents": [
                {"docid": docid, "text": retriever.docs[docid]}
                for docid in dict.fromkeys(str(d) for d in req.docids)
                if docid in retriever.docs
            ]
        }

    return app


def load_index(index_dir: str):
    """Merge Tevatron (reps, lookup) shards and check they are unit vectors."""
    paths = sorted(glob.glob(os.path.join(index_dir, "*.pkl")))
    if not paths:
        raise SystemExit(f"no index shards (*.pkl) in {index_dir}")
    reps, lookup = [], []
    for path in paths:
        with open(path, "rb") as f:
            shard_reps, shard_lookup = pickle.load(f)
        reps.append(np.asarray(shard_reps, dtype=np.float32))
        lookup.extend(str(docid) for docid in shard_lookup)
    matrix = np.concatenate(reps)
    norms = np.linalg.norm(matrix, axis=1)
    if not np.allclose(norms, 1.0, atol=1e-3):
        # The encoder output is normalized; an unnormalized index means the wrong file.
        raise SystemExit(f"index vectors are not unit length (min {norms.min():.4f}, max {norms.max():.4f})")
    if len(set(lookup)) != len(lookup):
        raise SystemExit("index contains duplicate docids")
    logger.info("index: %d docs x %d dims from %d shards", *matrix.shape, len(paths))
    return matrix, lookup


def load_corpus(source: str) -> Dict[str, str]:
    """Read docid -> text from the corpus parquet files.

    Reads the files setup.sh downloaded rather than calling datasets.load_dataset,
    which cannot open a snapshot_download'ed repo in offline mode.
    """
    import pyarrow.parquet as pq
    from huggingface_hub import snapshot_download

    root = source if os.path.isdir(source) else snapshot_download(source, repo_type="dataset")
    paths = sorted(glob.glob(os.path.join(root, "**", "*.parquet"), recursive=True))
    if not paths:
        raise SystemExit(f"no corpus parquet files under {root}")
    docs: Dict[str, str] = {}
    for path in paths:
        table = pq.read_table(path, columns=["docid", "text"])
        docs.update(zip(map(str, table.column("docid").to_pylist()), table.column("text").to_pylist()))
    return docs


def make_encoder(base: str, adapter: str, device: str, attn: str, max_len: int):
    """Return encode(query, name) for the base model and base + adapter."""
    import torch
    import torch.nn.functional as F
    from peft import PeftModel
    from transformers import AutoModel, AutoTokenizer

    dtype = torch.bfloat16 if device.startswith("cuda") else torch.float32
    tokenizer = AutoTokenizer.from_pretrained(base, padding_side="left")
    model = AutoModel.from_pretrained(base, dtype=dtype, attn_implementation=attn)
    # Kept unmerged so the same weights also serve the base encoder.
    model = PeftModel.from_pretrained(model, adapter).to(device).eval()
    lock = threading.Lock()

    @torch.no_grad()
    def forward(text: str) -> np.ndarray:
        batch = tokenizer(
            [QUERY_PREFIX + text], truncation=True, max_length=max_len, return_tensors="pt"
        ).to(device)
        hidden = model(**batch).last_hidden_state[:, -1]
        return F.normalize(hidden.float(), p=2, dim=-1)[0].cpu().numpy()

    def encode(text: str, name: str) -> np.ndarray:
        with lock:
            if name == OVERFIT_NAME:
                return forward(text)
            with model.disable_adapter():
                return forward(text)

    return encode


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-model", default=BASE_MODEL)
    ap.add_argument("--adapter", default=ADAPTER_REPO, help="HF repo or local dir of the LoRA adapter")
    ap.add_argument("--index-dir", required=True, help=f"directory with the {INDEX_SUBDIR} *.pkl shards")
    ap.add_argument("--corpus", default=CORPUS_REPO)
    ap.add_argument("--default-model", default=OVERFIT_NAME, choices=(OVERFIT_NAME, BASE_MODEL))
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--attn", default="sdpa")
    ap.add_argument("--max-len", type=int, default=512)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18200)
    ap.add_argument(
        "--api-token",
        default=os.environ.get("NEUTRON_API_TOKEN") or None,
        help="require 'Authorization: Bearer <token>' (default $NEUTRON_API_TOKEN; "
        "leave unset behind an authenticating proxy)",
    )
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    reps, lookup = load_index(args.index_dir)
    docs = load_corpus(args.corpus)
    missing = sum(docid not in docs for docid in lookup)
    if missing:
        raise SystemExit(f"{missing} indexed docids have no corpus text")
    logger.info("corpus: %d documents", len(docs))
    encode = make_encoder(args.base_model, args.adapter, args.device, args.attn, args.max_len)
    retriever = Retriever(reps, lookup, docs, encode, [OVERFIT_NAME, BASE_MODEL], args.default_model)

    import uvicorn

    uvicorn.run(build_app(retriever, args.api_token), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
