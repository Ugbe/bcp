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

import requests
from dotenv import load_dotenv

from .base import (
    BaseSearcher,
    normalize_novelty_request,
    select_novelty_results,
)

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

        self.session = requests.Session()
        if args.retrieval_token:
            self.session.headers["Authorization"] = f"Bearer {args.retrieval_token}"

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
        hits = payload.get("result", []) if isinstance(payload, dict) else payload
        if not isinstance(hits, list):
            return []

        results: List[Dict[str, Any]] = []
        for hit in hits:
            if not isinstance(hit, dict) or "docid" not in hit:
                continue
            document = hit.get("document") or {}
            results.append(
                {
                    "docid": str(hit["docid"]),
                    "score": hit.get("score"),
                    "text": (
                        hit.get("snippet")
                        or hit.get("text")
                        or document.get("text", "")
                    ),
                }
            )
        return results

    def search(self, query: str, k: int = 10) -> List[Dict[str, Any]]:
        # The server returns a fixed top-10; slice down to the caller's k.
        # Snippets are already capped at 512 tokens server-side.
        # Current authenticated hybrid API exposes /retrieve and wraps hits
        # in a {"result": [...]} object.
        response = self._request("POST", "/retrieve", json={"query": query})
        response.raise_for_status()
        return self._normalize_hits(response.json())[:k]

    def search_with_metadata(
        self,
        query: str,
        k: int = 10,
        *,
        exclude_docids: Optional[Iterable[str]] = None,
        seen_anchor_count: int = 0,
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
        response = self._request("GET", "/get_document", params={"docid": docid})
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json()
        return {"docid": str(data["docid"]), "text": data["text"]}

    @property
    def search_type(self) -> str:
        return "remote"
