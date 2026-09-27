"""
Remote API searcher: proxies search and get_document to the externally served
BrowseComp-Plus hybrid retrieval service (BM25 + dense + RRF + cross-encoder
rerank) described in bcp-replication/retrieval_api.md.

No local indexes, Java, FAISS or GPU are required -- every call is HTTP against
the remote server, authenticated with its instance token.
"""

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote

import requests
from dotenv import load_dotenv

from .base import (
    BaseSearcher,
    normalize_novelty_request,
    select_novelty_results,
)
from .kev_reranker import KevReranker

logger = logging.getLogger(__name__)

load_dotenv(Path(__file__).resolve().parents[2] / ".env")


class RemoteApiSearcher(BaseSearcher):
    """Searcher backed by the remote hybrid retrieval HTTP API."""

    @classmethod
    def parse_args(cls, parser):
        parser.add_argument(
            "--retrieval-url",
            default=os.environ.get("BCP_RETRIEVAL_URL", "http://127.0.0.1:17070"),
            help="Base URL of the remote retrieval service, e.g. http://host:port. "
            "Defaults to $BCP_RETRIEVAL_URL from the environment or .env.",
        )
        parser.add_argument(
            "--retrieval-token",
            default=os.environ.get("BCP_TOKEN"),
            help="Bearer token for the remote retrieval service. "
            "Defaults to $BCP_TOKEN from the environment or .env.",
        )
        parser.add_argument(
            "--retrieval-api",
            choices=("legacy", "dense", "agentir"),
            default=os.environ.get("BCP_RETRIEVAL_API", "legacy").strip().lower(),
            help=(
                "Remote retrieval API contract: 'legacy' uses /retrieve and "
                "/get_document; 'dense' uses /search, /document/{docid}, and "
                "/documents; 'agentir' uses the AgentIR-4B + Kev service "
                "(/search with the agent's reasoning, /document/{docid}, no "
                "batch endpoint). Defaults to $BCP_RETRIEVAL_API or legacy."
            ),
        )
        parser.add_argument(
            "--retrieval-reasoning-max-chars",
            type=int,
            default=int(os.environ.get("BCP_RETRIEVAL_REASONING_MAX_CHARS", "12000")),
            help=(
                "agentir API only: send at most this many trailing characters of "
                "the agent's reasoning with each search (0 disables). The server "
                "truncates its embedded text from the right, so an unbounded "
                "reasoning prefix would cut off the query itself."
            ),
        )
        parser.add_argument(
            "--retrieval-model",
            default=os.environ.get("BCP_RETRIEVAL_MODEL") or None,
            help=(
                "Optional encoder name sent to a dense retrieval service, such as "
                "browsecomp-overfit or Qwen/Qwen3-Embedding-0.6B."
            ),
        )
        parser.add_argument(
            "--kev-url",
            default=os.environ.get("BCP_KEV_URL") or None,
            help=(
                "Kev server base URL. When set, every search's hits are reordered "
                "by Kev's P(relevant) before the model sees them. Defaults to "
                "$BCP_KEV_URL; unset disables Kev."
            ),
        )
        parser.add_argument(
            "--kev-token",
            default=os.environ.get("BCP_KEV_TOKEN") or None,
            help="Bearer token for the Kev server. Defaults to $BCP_KEV_TOKEN.",
        )
        parser.add_argument(
            "--kev-doc-chars",
            type=int,
            default=int(os.environ.get("BCP_KEV_DOC_CHARS", "4096")),
            help="Leading characters of each document Kev judges (default 4096, "
            "about the 1024 tokens the AgentIR+Kev service sends).",
        )
        parser.add_argument(
            "--kev-timeout",
            type=float,
            default=float(os.environ.get("BCP_KEV_TIMEOUT", "120")),
            help="Per-document Kev request timeout in seconds (default 120).",
        )
        parser.add_argument(
            "--kev-log-path",
            default=os.environ.get("BCP_KEV_LOG_PATH") or None,
            help="Append one JSON line per search with retriever and Kev order. "
            "Defaults to $BCP_KEV_LOG_PATH.",
        )
        parser.add_argument(
            "--retrieval-timeout",
            type=float,
            default=60.0,
            help="HTTP timeout in seconds per request (default: 60, per "
            "bcp-replication/retrieval_api.md operational notes).",
        )
        parser.add_argument(
            "--retrieval-retries",
            type=int,
            default=3,
            help="Attempts per search request before surfacing an error (default: 3).",
        )
        parser.add_argument(
            "--retrieval-retry-backoff",
            type=float,
            default=5.0,
            help="Seconds to wait between retries, multiplied by the attempt "
            "number (default: 5).",
        )

    def __init__(self, args):
        self.url = args.retrieval_url.rstrip("/")
        self.timeout = args.retrieval_timeout
        self.retries = max(1, args.retrieval_retries)
        self.retry_backoff = args.retrieval_retry_backoff
        self.api = getattr(args, "retrieval_api", "legacy")
        self.model = getattr(args, "retrieval_model", None)
        self.reasoning_max_chars = max(
            0, int(getattr(args, "retrieval_reasoning_max_chars", 0) or 0)
        )

        self.session = requests.Session()
        if args.retrieval_token:
            self.session.headers["Authorization"] = f"Bearer {args.retrieval_token}"

        self.kev: Optional[KevReranker] = None
        kev_url = getattr(args, "kev_url", None)
        if kev_url:
            if self.api == "agentir":
                raise ValueError(
                    "the agentir service already reranks with Kev; unset BCP_KEV_URL"
                )
            self.kev = KevReranker(
                kev_url,
                token=getattr(args, "kev_token", None),
                timeout=getattr(args, "kev_timeout", 120.0),
                doc_chars=getattr(args, "kev_doc_chars", 4096),
                log_path=getattr(args, "kev_log_path", None),
            )

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        last_error: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.request(
                    method,
                    f"{self.url}{path}",
                    timeout=self.timeout,
                    **kwargs,
                )
                # Retry transient server-side failures; 4xx (auth, bad request)
                # are surfaced immediately since retrying cannot help.
                if response.status_code < 500:
                    return response
                last_error = requests.HTTPError(
                    f"{response.status_code} {response.reason}: {response.text[:200]}"
                )
                logger.warning(
                    "Retrieval service returned %s for %s (attempt %d/%d)",
                    response.status_code,
                    path,
                    attempt,
                    self.retries,
                )
            except requests.RequestException as e:
                last_error = e
                logger.warning(
                    "Retrieval request to %s failed (attempt %d/%d): %s",
                    path,
                    attempt,
                    self.retries,
                    e,
                )
            if attempt < self.retries:
                time.sleep(self.retry_backoff * attempt)
        raise ConnectionError(
            f"Retrieval service request to {path} failed after "
            f"{self.retries} attempts: {last_error}"
        )

    @staticmethod
    def _normalize_hits(payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, dict):
            hits = payload.get("result", payload.get("results", []))
        else:
            hits = payload
        if not isinstance(hits, list):
            return []

        results: List[Dict[str, Any]] = []
        for hit in hits:
            if not isinstance(hit, dict) or "docid" not in hit:
                continue
            document = hit.get("document") or {}
            item = {
                "docid": str(hit["docid"]),
                "score": hit.get("score"),
                "text": (
                    hit.get("snippet")
                    or hit.get("text")
                    or document.get("text", "")
                ),
            }
            title = document.get("title") or hit.get("title")
            if title:
                item["title"] = title
            results.append(item)
        return results

    @property
    def accepts_reasoning(self) -> bool:
        """True when searches should carry the agent's reasoning."""
        return self.api == "agentir" and self.reasoning_max_chars > 0

    def _dense_search_payload(
        self, query: str, k: int, reasoning: Optional[str] = None
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "query": query,
            "k": max(1, min(int(k), 1000)),
            "include_text": True,
        }
        if self.api == "agentir":
            reasoning = (reasoning or "").strip()
            if reasoning and self.accepts_reasoning:
                # Keep the tail: the thoughts right before the call explain it.
                payload["reasoning"] = reasoning[-self.reasoning_max_chars :]
        elif self.model:
            payload["model"] = self.model
        return payload

    def _search_request(
        self, query: str, k: int, reasoning: Optional[str] = None
    ) -> requests.Response:
        if self.api in ("dense", "agentir"):
            return self._request(
                "POST", "/search", json=self._dense_search_payload(query, k, reasoning)
            )
        return self._request("POST", "/retrieve", json={"query": query})

    @staticmethod
    def reciprocal_rank_fusion(
        result_lists: Iterable[Iterable[Dict[str, Any]]], *, k: int = 10, rrf_k: int = 60
    ) -> List[Dict[str, Any]]:
        """Fuse ranked result lists while retaining the best document payload.

        RRF is deliberately score-agnostic: the remote service's scores are not
        comparable across different query rewrites, but rank is stable enough
        for multi-query retrieval.  The returned ``score`` is the RRF score.
        """

        fused: Dict[str, Dict[str, Any]] = {}
        for result_list in result_lists:
            for rank, item in enumerate(result_list, start=1):
                if not isinstance(item, dict) or item.get("docid") is None:
                    continue
                docid = str(item["docid"])
                entry = fused.setdefault(docid, dict(item))
                entry["docid"] = docid
                entry["score"] = float(entry.get("score") or 0.0) + 1.0 / (
                    rrf_k + rank
                )
        return sorted(
            fused.values(),
            key=lambda item: (-float(item.get("score") or 0.0), str(item["docid"])),
        )[: max(1, int(k))]

    def search_multi_with_metadata(
        self,
        queries: Iterable[str],
        k: int = 10,
        *,
        exclude_docids: Optional[Iterable[str]] = None,
        seen_anchor_count: int = 0,
        reasoning: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run 2–4 query rewrites and fuse their ranked results with RRF."""

        normalized = []
        for query in queries:
            query = " ".join(str(query).split())
            if query and query not in normalized:
                normalized.append(query)
        if not 2 <= len(normalized) <= 4:
            raise ValueError("multi-query search requires between 2 and 4 distinct queries")

        result_lists = []
        metadata = []
        for query in normalized:
            response = self._retrieve_with_metadata(
                query,
                max(k, 10),
                exclude_docids=exclude_docids,
                seen_anchor_count=seen_anchor_count,
                reasoning=reasoning,
            )
            result_lists.append(response.get("results", []))
            metadata.append(response.get("metadata") or {})

        excluded = {str(item) for item in (exclude_docids or [])}
        fused = [item for item in self.reciprocal_rank_fusion(result_lists, k=max(k, 10)) if str(item.get("docid")) not in excluded]
        kev_metadata: Dict[str, Any] = {}
        if self.kev:
            # Rerank the fused selection once, not each rewrite's list.
            fused[:k], kev_metadata = self.kev.rerank(" || ".join(normalized), fused[:k])
        return {
            "results": fused[:k],
            "metadata": {
                **kev_metadata,
                "multi_query": True,
                "queries": normalized,
                "query_count": len(normalized),
                "rrf_k": 60,
                "exclusions_applied": any(item.get("exclusions_applied") for item in metadata),
                "fallback_applied": any(item.get("fallback_applied") for item in metadata),
                "novel_count": sum(str(item.get("docid")) not in excluded for item in fused[:k]),
                "repeated_count": sum(str(item.get("docid")) in excluded for item in fused[:k]),
                "requested_k": k,
            },
        }

    def search_pool(
        self,
        query: str,
        pool_k: int = 100,
        *,
        exclude_docids: Optional[Iterable[str]] = None,
        reasoning: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Request a larger ranked pool when the retrieval server supports it.

        Legacy authenticated servers return only ten hits and may reject the
        ``k`` field.  The result records that limitation instead of pretending
        a top-100 scan happened.
        """

        pool_k = max(10, min(int(pool_k), 1000))
        excluded = {str(item) for item in (exclude_docids or [])}
        response = self._search_request(query, pool_k, reasoning)
        if self.api == "legacy" and response.status_code in (400, 422):
            response = self._request("POST", "/retrieve", json={"query": query})
        response.raise_for_status()
        payload = response.json()
        candidates = self._normalize_hits(payload)
        filtered = [item for item in candidates if str(item.get("docid")) not in excluded]
        supported = len(candidates) >= pool_k or len(candidates) > 10
        return {
            "results": filtered[:pool_k],
            "metadata": {
                "deep_pool": True,
                "requested_pool_k": pool_k,
                "returned_count": len(candidates),
                "pool_supported": supported,
                "exclusions_applied": bool(excluded),
                "fallback_applied": not supported,
            },
        }

    def search(
        self, query: str, k: int = 10, *, reasoning: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        response = self._search_request(query, k, reasoning)
        response.raise_for_status()
        hits = self._normalize_hits(response.json())[:k]
        if self.kev:
            hits, _ = self.kev.rerank(query, hits)
        return hits

    def search_with_metadata(
        self,
        query: str,
        k: int = 10,
        *,
        exclude_docids: Optional[Iterable[str]] = None,
        seen_anchor_count: int = 0,
        reasoning: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Novelty-aware search, reordered by Kev when it is configured.

        Kev runs on the final selection, so it only reorders the documents the
        retriever and the novelty policy chose.
        """
        response = self._retrieve_with_metadata(
            query,
            k,
            exclude_docids=exclude_docids,
            seen_anchor_count=seen_anchor_count,
            reasoning=reasoning,
        )
        if self.kev:
            results, kev_metadata = self.kev.rerank(query, response["results"])
            response = {
                "results": results,
                "metadata": {**response["metadata"], **kev_metadata},
            }
        return response

    def _retrieve_with_metadata(
        self,
        query: str,
        k: int = 10,
        *,
        exclude_docids: Optional[Iterable[str]] = None,
        seen_anchor_count: int = 0,
        reasoning: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Use the novelty-aware API, with a safe fallback for old servers.

        An upgraded server applies exclusions before reranking and advertises
        that fact in ``metadata``.  An old server may ignore the new fields,
        return a legacy list/wrapper, or reject them with 400/422; all three cases
        are handled by filtering the returned top results locally.  That fallback
        can legitimately return fewer than ``k`` documents and reports this via
        ``candidate_pool_exhausted`` and ``fallback_applied``.
        """

        k, excluded, seen_anchor_count = normalize_novelty_request(
            k, exclude_docids, seen_anchor_count
        )
        if self.api in ("dense", "agentir"):
            # Neither service has server-side novelty controls. The dense one
            # can return a deep enough ranked list for an exact local
            # selection. AgentIR+Kev cannot: it reranks max(k, candidates)
            # documents with one Kev call each, so over-fetching would multiply
            # reranker load and change which candidates get reranked. Filter
            # its normal top k instead.
            fetch_k = k if self.api == "agentir" else min(1000, k + len(excluded))
            response = self._search_request(query, fetch_k, reasoning)
            response.raise_for_status()
            results, metadata = select_novelty_results(
                self._normalize_hits(response.json()),
                k=k,
                exclude_docids=excluded,
                seen_anchor_count=seen_anchor_count,
            )
            metadata.update(
                {
                    "fallback_applied": bool(excluded),
                    "server_exclusions_applied": False,
                    "retrieval_api": self.api,
                }
            )
            return {"results": results, "metadata": metadata}

        request_payload = {
            "query": query,
            "exclude_docids": sorted(excluded),
            "k": k,
            "seen_anchor_count": seen_anchor_count,
        }
        response = self._request("POST", "/retrieve", json=request_payload)

        # Some older Pydantic configurations reject unknown fields instead of
        # ignoring them.  Retry the legacy request shape only for schema errors;
        # authentication and other client errors must remain visible.
        retried_legacy_shape = False
        if response.status_code in (400, 422):
            retried_legacy_shape = True
            response = self._request("POST", "/retrieve", json={"query": query})

        response.raise_for_status()
        payload = response.json()
        candidates = self._normalize_hits(payload)
        server_metadata = payload.get("metadata") if isinstance(payload, dict) else None
        server_applied = bool(
            isinstance(server_metadata, dict)
            and server_metadata.get("exclusions_applied") is True
        )

        if server_applied:
            results = candidates[:k]
            metadata = dict(server_metadata)
            # Normalize mandatory fields so callers do not need to trust a
            # partially upgraded server implementation.
            metadata.update(
                {
                    "requested_k": k,
                    "novel_count": sum(
                        str(item.get("docid")) not in excluded for item in results
                    ),
                    "repeated_count": sum(
                        str(item.get("docid")) in excluded for item in results
                    ),
                    "excluded_count": len(excluded),
                    "candidate_pool_exhausted": len(results) < k,
                    "exclusions_applied": True,
                    "fallback_applied": False,
                }
            )
        else:
            results, metadata = select_novelty_results(
                candidates,
                k=k,
                exclude_docids=excluded,
                seen_anchor_count=seen_anchor_count,
            )
            metadata.update(
                {
                    "fallback_applied": True,
                    "legacy_request_retried": retried_legacy_shape,
                    "server_exclusions_applied": False,
                }
            )

        return {"results": results, "metadata": metadata}

    def get_document(self, docid: str) -> Optional[Dict[str, Any]]:
        if self.api in ("dense", "agentir"):
            response = self._request("GET", f"/document/{quote(str(docid), safe='')}")
        else:
            response = self._request("GET", "/get_document", params={"docid": docid})
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json()
        return {"docid": str(data["docid"]), "text": data["text"]}

    def get_documents(self, docids: Iterable[str]) -> List[Dict[str, Any]]:
        requested = list(dict.fromkeys(str(docid) for docid in docids))
        if not requested:
            return []
        if self.api != "dense":
            # legacy and agentir have no batch endpoint; fetch one at a time.
            return super().get_documents(requested)

        response = self._request("POST", "/documents", json={"docids": requested})
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("documents", []) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return []
        by_id = {
            str(row["docid"]): {"docid": str(row["docid"]), "text": row.get("text", "")}
            for row in rows
            if isinstance(row, dict) and row.get("docid") is not None
        }
        return [by_id[docid] for docid in requested if docid in by_id]

    @property
    def search_type(self) -> str:
        return "remote"
