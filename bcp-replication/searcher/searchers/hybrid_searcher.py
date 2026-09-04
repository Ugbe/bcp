"""Hybrid searcher: BM25 + dense candidates fused with RRF, then cross-encoder reranked.

Pipeline (see retrieval_api.md):

    query --+--> BM25 (Lucene)  --> top N --+
            |                               +--> RRF --> reranker --> top k
            +--> dense (FAISS)  --> top N --+

Both the query encoder and the reranker are LoRA-adapted Qwen3 models merged into
their bases at load time. The encoder config here must match the config the dense
index was built with (eos pooling, L2 normalization, fp16 forward) or the dense leg
returns noise -- see the index dataset card.
"""

import glob
import logging
import os
import pickle
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import faiss
import numpy as np
import torch
from datasets import load_dataset
from dotenv import load_dotenv
from peft import PeftModel
from transformers import AutoModel, AutoModelForCausalLM, AutoTokenizer

from .base import BaseSearcher

logger = logging.getLogger(__name__)

# The fine-tuned adapters live in private repos, so the token has to be present no
# matter which entrypoint (server, eval script) imported us.
load_dotenv(
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        ".env",
    )
)
if os.getenv("HF_TOKEN") and not os.getenv("HUGGING_FACE_HUB_TOKEN"):
    os.environ["HUGGING_FACE_HUB_TOKEN"] = os.environ["HF_TOKEN"]

# Reranker prompt scaffolding. This must match what the adapter was trained on.
RERANK_TASK = (
    "Given a web search query, retrieve relevant passages that answer the query"
)
RERANK_PREFIX = (
    "<|im_start|>system\nJudge whether the Document meets the requirements based on "
    'the Query and the Instruct provided. Note that the answer can only be "yes" or "no".'
    "<|im_end|>\n<|im_start|>user\n"
)
RERANK_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"

# The rerank body is head + document, in that order. Because the query lives
# entirely in the head, the document half is query-independent and can be
# tokenized once offline -- see scripts_build_index/build_rerank_token_cache.py.
# These two must stay in sync with that script's DOC_PREFIX.
RERANK_DOC_PREFIX = "\n<Document>: "
MAX_EXCLUDE_DOCIDS = 2000


def _rerank_head(query: str) -> str:
    return f"<Instruct>: {RERANK_TASK}\n<Query>: {query}"


class HybridSearcher(BaseSearcher):
    @classmethod
    def parse_args(cls, parser):
        parser.add_argument(
            "--index-path",
            required=True,
            help="Glob for dense index shards, e.g. indexes/qwen3-embedding-8b-finetuned-v2/corpus.shard*.pkl",
        )
        parser.add_argument(
            "--bm25-index-path",
            required=True,
            help="Path to the Lucene BM25 index directory.",
        )
        parser.add_argument(
            "--embedding-model",
            default="Qwen/Qwen3-Embedding-8B",
            help="Base bi-encoder.",
        )
        parser.add_argument(
            "--embedding-lora",
            default="DanielTobi0/qwen3-embedding-8b-browsecomp-lora-v2",
            help="LoRA adapter merged into the bi-encoder. Must be the adapter that built the index.",
        )
        parser.add_argument(
            "--reranker-model",
            default="Qwen/Qwen3-Reranker-0.6B",
            help="Base cross-encoder.",
        )
        parser.add_argument(
            "--reranker-lora",
            default="DanielTobi0/qwen3-reranker-0.6b-browsecomp-lora-v3",
            help="LoRA adapter merged into the cross-encoder.",
        )
        parser.add_argument(
            "--dataset-name",
            default="Tevatron/browsecomp-plus-corpus",
            help="Corpus used to resolve docid -> text.",
        )
        parser.add_argument(
            "--candidates-k",
            type=int,
            default=100,
            help="Candidates taken from each retrieval leg before fusion (default: 100).",
        )
        parser.add_argument(
            "--rrf-k",
            type=int,
            default=60,
            help="RRF smoothing constant (default: 60).",
        )
        parser.add_argument(
            "--rerank-depth",
            type=int,
            default=100,
            help="How many fused candidates to rerank (default: 100). Higher = better recall, slower.",
        )
        parser.add_argument(
            "--rerank-token-budget",
            type=int,
            default=16384,
            help="Max padded tokens per cross-encoder batch (default: 16384).",
        )
        parser.add_argument(
            "--rerank-max-batch",
            type=int,
            default=64,
            help="Max documents per cross-encoder batch (default: 64). The token "
            "budget is normally the binding constraint; this is a safety cap.",
        )
        parser.add_argument(
            "--rerank-token-cache",
            default=None,
            help="Directory of pre-tokenized documents (indexes/rerank-token-cache). "
            "Removes ~2.2s/query of tokenization. Falls back to live tokenization "
            "when unset or missing.",
        )
        parser.add_argument(
            "--rerank-stage1-length",
            type=int,
            default=512,
            help="Cascade stage 1: token cap used to score every candidate cheaply "
            "(default: 512). Set --disable-cascade to score everything at full length.",
        )
        parser.add_argument(
            "--rerank-stage1-keep",
            type=int,
            default=35,
            help="Cascade stage 2: how many of stage 1's top candidates get rescored "
            "at --rerank-max-length (default: 35).",
        )
        parser.add_argument(
            "--disable-cascade",
            action="store_true",
            help="Score every candidate at full --rerank-max-length, as before. "
            "Much slower; kept for quality comparisons.",
        )
        parser.add_argument(
            "--rerank-max-length",
            type=int,
            default=8192,
            help="Cross-encoder context length (default: 8192, the length it was trained at).",
        )
        parser.add_argument(
            "--query-max-length",
            type=int,
            default=4096,
            help="Bi-encoder query length (default: 4096, matching index build).",
        )
        parser.add_argument(
            "--task-prefix",
            default=(
                "Instruct: Given a web search query, retrieve relevant passages that "
                "answer the query\nQuery:"
            ),
            help="Instruction prefix prepended to queries before encoding.",
        )
        parser.add_argument(
            "--attn-implementation",
            default="flash_attention_2",
            help="Attention kernel (flash_attention_2, sdpa, eager). Falls back to "
            "sdpa automatically if flash-attn is not importable.",
        )
        parser.add_argument(
            "--disable-rerank",
            action="store_true",
            help="Return the RRF-fused order directly. For diagnostics.",
        )
        parser.add_argument(
            "--disable-bm25",
            action="store_true",
            help="Dense leg only. For diagnostics.",
        )
        parser.add_argument(
            "--disable-dense",
            action="store_true",
            help="BM25 leg only. For diagnostics.",
        )

    def __init__(self, args):
        self.args = args
        # Query embedding and reranking both run on the GPU; serialize them so
        # concurrent requests queue instead of racing into CUDA OOM.
        self.gpu_lock = threading.Lock()
        # Runs the BM25 leg alongside the dense leg. Small: it exists to overlap two
        # legs per request, not to add request-level concurrency.
        self._cpu_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bm25")

        self.bm25 = None
        self.dense_index = None
        self.lookup: List[str] = []
        self.docid_to_text: Dict[str, str] = {}

        # Pre-tokenized document cache; None means fall back to live tokenization.
        self.tok_cache = None
        self.tok_offsets = None
        self.tok_row: Dict[str, int] = {}

        self.args.attn_implementation = self._resolve_attn(args.attn_implementation)

        self._load_corpus()
        if not args.disable_bm25:
            self._load_bm25()
        if not args.disable_dense:
            self._load_dense_index()
            self._load_encoder()
        if not args.disable_rerank:
            self._load_reranker()
            self._load_token_cache()

        logger.info("Hybrid searcher ready")

    # ---------------------------------------------------------------- loading

    @staticmethod
    def _resolve_attn(requested: str) -> str:
        """Downgrade to sdpa rather than dying if flash-attn is not installed."""
        if requested != "flash_attention_2":
            return requested
        try:
            import flash_attn  # noqa: F401
        except ImportError:
            logger.warning("flash-attn not importable, falling back to sdpa")
            return "sdpa"
        return requested

    def _load_token_cache(self) -> None:
        path = self.args.rerank_token_cache
        if not path:
            logger.info("No rerank token cache configured; tokenizing per request")
            return
        if not os.path.isdir(path):
            logger.warning("Rerank token cache %s missing; tokenizing per request", path)
            return

        import json

        with open(os.path.join(path, "docids.json")) as fh:
            meta = json.load(fh)

        # A cache built with a different tokenizer or a shorter cap would silently
        # feed the reranker wrong or truncated ids, so refuse it instead.
        if meta["tokenizer"] != self.args.reranker_lora:
            logger.warning(
                "Token cache built with %s but reranker is %s; ignoring cache",
                meta["tokenizer"],
                self.args.reranker_lora,
            )
            return
        if meta["max_doc_tokens"] < self.args.rerank_max_length:
            logger.warning(
                "Token cache caps documents at %d tokens < rerank-max-length %d; "
                "ignoring cache",
                meta["max_doc_tokens"],
                self.args.rerank_max_length,
            )
            return
        if meta["doc_prefix"] != RERANK_DOC_PREFIX:
            logger.warning("Token cache doc prefix differs from this build; ignoring")
            return

        self.tok_offsets = np.load(os.path.join(path, "offsets.npy"))
        self.tok_cache = np.memmap(
            os.path.join(path, "tokens.i32"), dtype=np.int32, mode="r"
        )
        self.tok_row = {docid: i for i, docid in enumerate(meta["docids"])}
        logger.info(
            "Rerank token cache loaded: %d documents, %.2f GB",
            len(self.tok_row),
            meta["n_tokens"] * 4 / 1e9,
        )

    def _load_corpus(self) -> None:
        logger.info("Loading corpus %s", self.args.dataset_name)
        ds = load_dataset(self.args.dataset_name, split="train")
        self.docid_to_text = {row["docid"]: row["text"] for row in ds}
        logger.info("Corpus loaded: %d documents", len(self.docid_to_text))

    def _load_bm25(self) -> None:
        from pyserini.search.lucene import LuceneSearcher

        logger.info("Loading BM25 index %s", self.args.bm25_index_path)
        self.bm25 = LuceneSearcher(self.args.bm25_index_path)
        if self.bm25 is None:
            raise ValueError(f"Invalid Lucene index: {self.args.bm25_index_path}")
        logger.info("BM25 index loaded: %d docs", self.bm25.num_docs)

    def _load_dense_index(self) -> None:
        shard_paths = sorted(glob.glob(self.args.index_path))
        if not shard_paths:
            raise ValueError(f"No dense index shards match {self.args.index_path}")

        logger.info("Loading %d dense index shards", len(shard_paths))
        all_reps, self.lookup = [], []
        for path in shard_paths:
            with open(path, "rb") as fh:
                reps, docids = pickle.load(fh)
            all_reps.append(np.asarray(reps, dtype=np.float32))
            self.lookup.extend(docids)

        reps = np.concatenate(all_reps, axis=0)
        del all_reps

        norms = np.linalg.norm(reps, axis=1)
        logger.info(
            "Dense index: %d vectors, dim %d, norms mean=%.4f min=%.4f max=%.4f",
            reps.shape[0],
            reps.shape[1],
            float(norms.mean()),
            float(norms.min()),
            float(norms.max()),
        )

        # Inner product over L2-normalized vectors == cosine similarity.
        self.dense_index = faiss.IndexFlatIP(reps.shape[1])
        self.dense_index.add(reps)
        logger.info("FAISS index built (CPU, flat IP)")

    def _merge_lora(self, base, adapter: str):
        model = PeftModel.from_pretrained(base, adapter)
        return model.merge_and_unload()

    def _load_encoder(self) -> None:
        logger.info(
            "Loading bi-encoder %s + %s",
            self.args.embedding_model,
            self.args.embedding_lora,
        )
        base = AutoModel.from_pretrained(
            self.args.embedding_model,
            dtype=torch.bfloat16,
            attn_implementation=self.args.attn_implementation,
        )
        self.encoder = self._merge_lora(base, self.args.embedding_lora).to("cuda").eval()
        self.encoder_tok = AutoTokenizer.from_pretrained(
            self.args.embedding_lora, padding_side="right"
        )
        if self.encoder_tok.pad_token is None:
            self.encoder_tok.pad_token = self.encoder_tok.eos_token
        logger.info("Bi-encoder ready")

    def _load_reranker(self) -> None:
        logger.info(
            "Loading cross-encoder %s + %s",
            self.args.reranker_model,
            self.args.reranker_lora,
        )
        base = AutoModelForCausalLM.from_pretrained(
            self.args.reranker_model,
            dtype=torch.bfloat16,
            attn_implementation=self.args.attn_implementation,
        )
        self.reranker = self._merge_lora(base, self.args.reranker_lora).to("cuda").eval()
        self.reranker_tok = AutoTokenizer.from_pretrained(
            self.args.reranker_lora, padding_side="left"
        )
        if self.reranker_tok.pad_token is None:
            self.reranker_tok.pad_token = self.reranker_tok.eos_token

        self.rerank_prefix_ids = self.reranker_tok.encode(
            RERANK_PREFIX, add_special_tokens=False
        )
        self.rerank_suffix_ids = self.reranker_tok.encode(
            RERANK_SUFFIX, add_special_tokens=False
        )
        self.yes_id = self.reranker_tok.convert_tokens_to_ids("yes")
        self.no_id = self.reranker_tok.convert_tokens_to_ids("no")
        logger.info("Cross-encoder ready")

    # ------------------------------------------------------------- retrieval

    @torch.no_grad()
    def _encode_query(self, query: str) -> np.ndarray:
        text = self.args.task_prefix + query

        # Do NOT append an EOS token by hand. This tokenizer already terminates the
        # sequence with <|endoftext|> under add_special_tokens=True, and that is the
        # position the index was pooled at. Note tok.eos_token is <|im_end|>, a
        # *different* token -- appending it pools from the wrong position and measurably
        # drops recall (0.65 vs 0.76 recall@100 on a 10-query probe).
        enc = self.encoder_tok(
            text,
            truncation=True,
            max_length=self.args.query_max_length,
            add_special_tokens=True,
            return_attention_mask=False,
        )

        batch = self.encoder_tok.pad(
            {"input_ids": [enc["input_ids"]]},
            return_attention_mask=True,
            return_tensors="pt",
        )
        batch = {k: v.to("cuda") for k, v in batch.items()}

        with torch.amp.autocast("cuda", dtype=torch.float16):
            hidden = self.encoder(**batch).last_hidden_state

        # eos pooling: hidden state of the final non-padding token.
        last_idx = batch["attention_mask"].sum(dim=1) - 1
        reps = hidden[torch.arange(hidden.size(0), device=hidden.device), last_idx]
        reps = torch.nn.functional.normalize(reps.float(), p=2, dim=-1)
        return reps.cpu().numpy().astype(np.float32)

    def _dense_search(self, query: str, k: int) -> List[str]:
        q = self._encode_query(query)
        _, indices = self.dense_index.search(q, k)
        return [self.lookup[i] for i in indices[0] if i >= 0]

    def _bm25_search(self, query: str, k: int) -> List[str]:
        try:
            hits = self.bm25.search(query, k)
        except Exception as exc:
            logger.warning("BM25 search failed, continuing dense-only: %s", exc)
            return []
        return [hit.docid for hit in hits]

    @staticmethod
    def _rrf(rankings: List[List[str]], rrf_k: int) -> List[Tuple[str, float]]:
        scores: Dict[str, float] = {}
        for ranking in rankings:
            for rank, docid in enumerate(ranking, start=1):
                scores[docid] = scores.get(docid, 0.0) + 1.0 / (rrf_k + rank)
        return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)

    @torch.no_grad()
    def _score_batch(self, batch: List[Tuple[str, List[int]]]) -> List[Tuple[str, float]]:
        """Score one padded batch, halving it rather than failing on CUDA OOM.

        The token budget is tuned for headroom, but it is a static guess against a
        shared GPU: fragmentation or anything else resident can still push a large
        batch over. Splitting costs a little latency on the rare batch that trips;
        propagating the error would 500 the whole request.
        """
        try:
            return self._score_batch_inner(batch)
        except torch.OutOfMemoryError:
            if len(batch) == 1:
                raise
            torch.cuda.empty_cache()
            mid = len(batch) // 2
            logger.warning(
                "CUDA OOM on a %d-document rerank batch (longest %d tokens); "
                "retrying as %d + %d",
                len(batch),
                max(len(ids) for _, ids in batch),
                mid,
                len(batch) - mid,
            )
            return self._score_batch(batch[:mid]) + self._score_batch(batch[mid:])

    @torch.no_grad()
    def _score_batch_inner(
        self, batch: List[Tuple[str, List[int]]]
    ) -> List[Tuple[str, float]]:
        inputs = self.reranker_tok.pad(
            {"input_ids": [ids for _, ids in batch]},
            padding=True,
            return_attention_mask=True,
            return_tensors="pt",
        )
        inputs = {k: v.to("cuda") for k, v in inputs.items()}

        try:
            logits = self.reranker(**inputs, logits_to_keep=1).logits[:, -1, :]
        except TypeError:  # older signature without logits_to_keep
            logits = self.reranker(**inputs).logits[:, -1, :]

        pair = torch.stack([logits[:, self.no_id], logits[:, self.yes_id]], dim=1)
        probs = torch.log_softmax(pair.float(), dim=1)[:, 1].exp()
        return list(zip([docid for docid, _ in batch], probs.tolist()))

    def _doc_token_ids(self, docid: str, limit: int) -> List[int]:
        """Token ids of RERANK_DOC_PREFIX + document, capped at `limit`."""
        if limit <= 0:
            return []
        if self.tok_cache is not None:
            row = self.tok_row.get(docid)
            if row is not None:
                start = int(self.tok_offsets[row])
                end = min(int(self.tok_offsets[row + 1]), start + limit)
                return self.tok_cache[start:end].tolist()

        # Fallback path (no cache, or a docid the cache predates). Cut the raw string
        # first: some documents are >100k tokens and tokenizing them in full just to
        # discard the tail is what this cache exists to avoid. 8 chars/token safely
        # overestimates the tokenizer's ratio.
        doc = self.docid_to_text.get(docid, "")[: limit * 8]
        return self.reranker_tok(
            RERANK_DOC_PREFIX + doc,
            truncation="longest_first",
            max_length=limit,
            add_special_tokens=False,
        )["input_ids"]

    def _encode_body_live(self, head: str, docid: str, body_budget: int) -> List[int]:
        """Tokenize head+document as one sequence, exactly as the pre-cache code did."""
        doc = self.docid_to_text.get(docid, "")[: body_budget * 8]
        return self.reranker_tok(
            head + RERANK_DOC_PREFIX + doc,
            truncation="longest_first",
            max_length=body_budget,
            add_special_tokens=False,
        )["input_ids"]

    def _encode_pairs(
        self, query: str, docids: List[str], max_length: int
    ) -> List[Tuple[str, List[int]]]:
        body_budget = (
            max_length - len(self.rerank_prefix_ids) - len(self.rerank_suffix_ids)
        )
        head = _rerank_head(query)

        # Splitting one tokenization in two is only equivalent if no BPE merge spans
        # the join. RERANK_DOC_PREFIX starts with "\n", which never merges backwards
        # into a word -- but it does merge with a preceding *whitespace* character
        # ("Ġ" + "Ċ" -> "ĠĊ"). That happens when the query ends in whitespace or is
        # empty, so fall back to tokenizing the whole body for those queries rather
        # than silently feeding the reranker a different token stream.
        if self.tok_cache is None or (head and head[-1].isspace()):
            return [
                (
                    docid,
                    self.rerank_prefix_ids
                    + self._encode_body_live(head, docid, body_budget)
                    + self.rerank_suffix_ids,
                )
                for docid in docids
            ]

        head_ids = self.reranker_tok(head, add_special_tokens=False)["input_ids"]
        # Mirrors truncating head+document as one sequence: the head is what survives
        # when a pathologically long query eats the whole budget.
        head_ids = head_ids[:body_budget]
        avail = body_budget - len(head_ids)

        return [
            (
                docid,
                self.rerank_prefix_ids
                + head_ids
                + self._doc_token_ids(docid, avail)
                + self.rerank_suffix_ids,
            )
            for docid in docids
        ]

    def _score(
        self, query: str, docids: List[str], max_length: int
    ) -> List[Tuple[str, float]]:
        """Score every (query, doc) pair at `max_length`, best first."""
        if not docids:
            return []

        encoded = self._encode_pairs(query, docids, max_length)

        # Batch length-sorted: padding every document up to the longest one in an
        # arbitrary batch wastes ~half the compute on this corpus, where lengths span
        # two orders of magnitude.
        encoded.sort(key=lambda item: len(item[1]))

        scored: List[Tuple[str, float]] = []
        batch: List[Tuple[str, List[int]]] = []
        for item in encoded:
            longest = len(item[1])  # ascending order, so this is the batch max
            over_budget = longest * (len(batch) + 1) > self.args.rerank_token_budget
            if batch and (over_budget or len(batch) >= self.args.rerank_max_batch):
                scored.extend(self._score_batch(batch))
                batch = []
            batch.append(item)
        if batch:
            scored.extend(self._score_batch(batch))

        scored.sort(key=lambda kv: kv[1], reverse=True)
        return scored

    def _rerank(self, query: str, docids: List[str]) -> List[Tuple[str, float]]:
        """Cascade rerank: cheap pass over everything, full-length pass over the top.

        Scoring all 200 candidates at 8192 tokens costs ~716k tokens per query and
        dominates end-to-end latency. Stage 1 scores every candidate at
        --rerank-stage1-length (~7x cheaper per document) purely to decide what is
        worth a close look; stage 2 rescores the survivors at full length, so the
        documents that actually reach the top-10 are still judged exactly as before.
        """
        if self.args.disable_cascade:
            return self._score(query, docids, self.args.rerank_max_length)

        t0 = time.perf_counter()
        stage1 = self._score(query, docids, self.args.rerank_stage1_length)
        t1 = time.perf_counter()
        keep = [docid for docid, _ in stage1[: self.args.rerank_stage1_keep]]
        stage2 = self._score(query, keep, self.args.rerank_max_length)
        logger.info(
            "rerank: stage1 %d docs %.0fms, stage2 %d docs %.0fms",
            len(docids),
            (t1 - t0) * 1000,
            len(keep),
            (time.perf_counter() - t1) * 1000,
        )

        # Stage-1 and stage-2 scores are not comparable -- a document read at 512
        # tokens and one read at 8192 are answering different questions -- so never
        # interleave them. Everything rescored at full length outranks everything
        # that was only ever seen through the cheap pass.
        rescored = set(keep)
        tail = [(docid, score) for docid, score in stage1 if docid not in rescored]
        return stage2 + tail

    @staticmethod
    def _select_ranked_with_novelty(
        ranked: List[Tuple[str, float]],
        *,
        k: int,
        excluded: Set[str],
        seen_anchor_count: int,
    ) -> List[Tuple[str, float]]:
        """Select novel rows and optional seen anchors in stable ranked order."""

        novel = [item for item in ranked if str(item[0]) not in excluded]
        seen = [item for item in ranked if str(item[0]) in excluded]
        novel_target = max(0, k - seen_anchor_count)
        chosen_novel = novel[:novel_target]
        chosen_seen = seen[:seen_anchor_count]

        # Anchor count is a maximum.  Fill missing anchor slots with novel hits.
        remaining = k - len(chosen_novel) - len(chosen_seen)
        if remaining > 0:
            chosen_novel.extend(
                novel[len(chosen_novel) : len(chosen_novel) + remaining]
            )

        chosen_docids = {docid for docid, _ in chosen_novel + chosen_seen}
        return [item for item in ranked if item[0] in chosen_docids][:k]

    def search(self, query: str, k: int = 10) -> List[Dict[str, Any]]:
        """Legacy list-returning search API."""

        return self.search_with_metadata(query, k=k)["results"]

    def search_with_metadata(
        self,
        query: str,
        k: int = 10,
        *,
        exclude_docids: Optional[Iterable[str]] = None,
        seen_anchor_count: int = 0,
    ) -> Dict[str, Any]:
        """Search with candidate-stage exclusions and compact novelty metadata."""

        if isinstance(k, bool) or not isinstance(k, int) or k < 1:
            raise ValueError("k must be a positive integer")
        if (
            isinstance(seen_anchor_count, bool)
            or not isinstance(seen_anchor_count, int)
            or seen_anchor_count < 0
            or seen_anchor_count > k
        ):
            raise ValueError("seen_anchor_count must be between 0 and k")
        excluded = {str(docid) for docid in (exclude_docids or [])}
        if len(excluded) > MAX_EXCLUDE_DOCIDS:
            raise ValueError(
                f"exclude_docids may contain at most {MAX_EXCLUDE_DOCIDS} unique IDs"
            )

        # Over-fetch each cheap first-stage retrieval leg enough to replace likely
        # exclusions.  The exclusion list is server-bounded, and only the filtered
        # rerank-depth plus optional anchors reaches the expensive cross-encoder.
        requested_pool = max(
            self.args.candidates_k,
            self.args.rerank_depth + len(excluded),
            k + len(excluded),
        )
        n = min(len(self.docid_to_text), requested_pool)

        # Only the encoder forward and the reranker touch the GPU. BM25 runs on the
        # JVM and the FAISS scan on CPU, and both release the GIL, so run BM25
        # alongside the dense leg and keep the GPU lock off everything that does not
        # need it -- under concurrent load one caller's BM25 now overlaps another's
        # rerank instead of queueing behind it.
        t = {}
        t0 = time.perf_counter()

        bm25_future = None
        if not self.args.disable_bm25:
            bm25_future = self._cpu_pool.submit(self._bm25_search, query, n)

        dense: Optional[List[str]] = None
        if not self.args.disable_dense:
            with self.gpu_lock:
                t["gpu_wait+encode"] = time.perf_counter() - t0
                encoded_query = self._encode_query(query)
            mark = time.perf_counter()
            t["encode"] = mark - t0 - t.get("gpu_wait+encode", 0.0)
            _, indices = self.dense_index.search(encoded_query, n)
            dense = [self.lookup[i] for i in indices[0] if i >= 0]
            t["faiss"] = time.perf_counter() - mark

        # BM25 first, then dense, matching the fusion order this pipeline was tuned
        # with (RRF itself is order-independent, but tie-breaking is not).
        mark = time.perf_counter()
        rankings = []
        if bm25_future is not None:
            rankings.append(bm25_future.result())
        if dense is not None:
            rankings.append(dense)
        t["bm25_join"] = time.perf_counter() - mark

        fused = self._rrf(rankings, self.args.rrf_k)
        novel_fused = [item for item in fused if str(item[0]) not in excluded]
        seen_fused = [item for item in fused if str(item[0]) in excluded]
        rerank_depth = max(k, self.args.rerank_depth)
        candidate_docids = {
            docid for docid, _ in novel_fused[:rerank_depth]
        } | {
            docid for docid, _ in seen_fused[:seen_anchor_count]
        }
        filtered_fused = [item for item in fused if item[0] in candidate_docids]

        if self.args.disable_rerank:
            ranked_pool = filtered_fused
        else:
            candidates = [docid for docid, _ in filtered_fused]
            mark = time.perf_counter()
            with self.gpu_lock:
                t["rerank_wait"] = time.perf_counter() - mark
                ranked_pool = self._rerank(query, candidates)
            t["rerank"] = time.perf_counter() - mark - t["rerank_wait"]

        ranked = self._select_ranked_with_novelty(
            ranked_pool,
            k=k,
            excluded=excluded,
            seen_anchor_count=seen_anchor_count,
        )

        mark = time.perf_counter()
        results = [
            {
                "docid": docid,
                "score": float(score),
                "text": self.docid_to_text.get(docid, "Text not found"),
            }
            for docid, score in ranked
        ]
        t["collect"] = time.perf_counter() - mark
        t["total"] = time.perf_counter() - t0
        logger.info(
            "search timing: %s",
            " ".join(f"{key}={value * 1000:.0f}ms" for key, value in t.items()),
        )
        metadata = {
            "requested_k": k,
            "novel_count": sum(
                str(result["docid"]) not in excluded for result in results
            ),
            "repeated_count": sum(
                str(result["docid"]) in excluded for result in results
            ),
            "excluded_count": len(excluded),
            "candidate_pool_exhausted": len(results) < k,
            "exclusions_applied": True,
            "fallback_applied": False,
            "filtered_candidate_count": len(seen_fused) - min(
                len(seen_fused), seen_anchor_count
            ),
            "overfetch_k": n,
        }
        return {"results": results, "metadata": metadata}

    def get_document(self, docid: str) -> Optional[Dict[str, Any]]:
        text = self.docid_to_text.get(docid)
        if text is None:
            return None
        return {"docid": docid, "text": text}

    @property
    def search_type(self) -> str:
        return "Hybrid(BM25+Dense,RRF,reranked)"

    def search_description(self, k: int = 10) -> str:
        return (
            f"Search the BrowseComp-Plus web corpus. Returns top-{k} documents ranked by a "
            "hybrid BM25 + dense retriever with cross-encoder reranking."
        )
