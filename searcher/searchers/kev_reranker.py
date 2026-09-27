"""Client-side Kev reranking of a retriever's hits.

Kev (github.com/jaredpalmer/kev) answers one yes/no question per document --
"does this passage help answer the query?" -- through POST /v1/systemone and
returns P(yes). Reranking a hit list with it never changes which documents are
returned, only their order: hits are sorted by P(yes), and the retriever's rank
breaks ties. A document Kev fails to score keeps its place behind every scored
one, so one failed call cannot fail a search; ``kev_scored`` in the metadata and
the per-search log make such fallbacks visible.

The question, the ``noul`` answer type, and the prefix-of-document state match
the AgentIR+Kev retrieval service, so Kev sees the same kind of request whether
it runs behind that service or here.
"""

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

KEV_QUESTION = (
    "A researcher ran the web search query: {query}\n\n"
    "Does this passage contain information that helps answer that query -- i.e. it is "
    "about the entity, event or fact being searched for, or matches its specific details?"
)


class KevReranker:
    def __init__(
        self,
        url: str,
        *,
        token: Optional[str] = None,
        timeout: float = 120.0,
        retries: int = 2,
        doc_chars: int = 4096,
        log_path: Optional[str] = None,
        max_workers: int = 16,
    ):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self.doc_chars = max(1, int(doc_chars))
        self.log_path = log_path
        self.session = requests.Session()
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        self._log_lock = threading.Lock()

    def _payload(self, query: str, text: str) -> Dict[str, Any]:
        return {
            "model": "kev-latest",
            # Kev judges a prefix; it was trained on short states.
            "state": text[: self.doc_chars],
            "questions": {
                "relevant": {
                    "type": "noul",
                    "instructions": KEV_QUESTION.format(query=query),
                }
            },
        }

    def score(self, query: str, text: str) -> Optional[float]:
        """P(yes) for one document, or None when Kev cannot be reached."""
        last_error: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                response = self.session.post(
                    f"{self.url}/v1/systemone",
                    json=self._payload(query, text),
                    timeout=self.timeout,
                )
                response.raise_for_status()
                return float(response.json()["answers"]["relevant"]["noul"])
            except Exception as e:  # noqa: BLE001 - any failure falls back per doc
                last_error = e
        logger.warning("Kev scoring failed after %d attempts: %s", self.retries, last_error)
        return None

    def rerank(
        self, query: str, hits: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Return the same hits ordered by Kev, plus Kev metadata."""
        started = time.perf_counter()
        texts = [str(hit.get("text") or hit.get("snippet") or "") for hit in hits]
        probs = list(self._pool.map(lambda text: self.score(query, text), texts))
        order = sorted(
            range(len(hits)),
            key=lambda i: (-(probs[i] if probs[i] is not None else -1.0), i),
        )
        reranked = []
        for i in order:
            hit = dict(hits[i])
            hit["retriever_score"] = hits[i].get("score")
            hit["retriever_rank"] = i + 1
            hit["kev_prob"] = probs[i]
            # The model sees the score that decided the order, as the AgentIR
            # service reports it; retriever scores would appear out of order.
            if probs[i] is not None:
                hit["score"] = probs[i]
            reranked.append(hit)

        kev_ms = round((time.perf_counter() - started) * 1000)
        metadata = {
            "kev_requested": len(hits),
            "kev_scored": sum(p is not None for p in probs),
            "kev_ms": kev_ms,
        }
        self._log(query, hits, probs, reranked, metadata)
        return reranked, metadata

    def _log(self, query, hits, probs, reranked, metadata) -> None:
        if not self.log_path:
            return
        record = {
            "ts": time.time(),
            "query": query,
            "retrieved": [
                {"docid": str(hit.get("docid")), "score": hit.get("score"), "kev": prob}
                for hit, prob in zip(hits, probs)
            ],
            "returned": [str(hit.get("docid")) for hit in reranked],
            **metadata,
        }
        try:
            with self._log_lock, open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError as e:
            logger.warning("Could not write Kev log %s: %s", self.log_path, e)
