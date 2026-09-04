"""
Abstract base class for search implementations.
"""

import argparse
from abc import ABC, abstractmethod
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


MAX_EXCLUDE_DOCIDS = 2000


def normalize_novelty_request(
    k: int,
    exclude_docids: Optional[Iterable[str]] = None,
    seen_anchor_count: int = 0,
) -> Tuple[int, Set[str], int]:
    """Validate and normalize the optional novelty-search parameters.

    This helper is intentionally transport-agnostic.  HTTP callers turn the
    ``ValueError`` into a 422 response, while in-process callers receive a clear
    programming error instead of silently issuing an unexpectedly large search.
    """

    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError("k must be a positive integer")
    if (
        isinstance(seen_anchor_count, bool)
        or not isinstance(seen_anchor_count, int)
        or seen_anchor_count < 0
        or seen_anchor_count > k
    ):
        raise ValueError("seen_anchor_count must be an integer between 0 and k")

    normalized = {str(docid) for docid in (exclude_docids or [])}
    if len(normalized) > MAX_EXCLUDE_DOCIDS:
        raise ValueError(
            f"exclude_docids may contain at most {MAX_EXCLUDE_DOCIDS} unique IDs"
        )
    return k, normalized, seen_anchor_count


def select_novelty_results(
    candidates: List[Dict[str, Any]],
    *,
    k: int,
    exclude_docids: Set[str],
    seen_anchor_count: int = 0,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Select novel hits plus optional seen anchors without changing rank order."""

    novel = [c for c in candidates if str(c.get("docid")) not in exclude_docids]
    seen = [c for c in candidates if str(c.get("docid")) in exclude_docids]

    novel_target = max(0, k - seen_anchor_count)
    chosen_novel = novel[:novel_target]
    chosen_seen = seen[:seen_anchor_count]

    # ``seen_anchor_count`` is a maximum, not a reservation.  If fewer anchors
    # are available, fill the response with additional novel documents.
    remaining = k - len(chosen_novel) - len(chosen_seen)
    if remaining > 0:
        chosen_novel.extend(novel[len(chosen_novel) : len(chosen_novel) + remaining])

    chosen_ids = {id(item) for item in chosen_novel + chosen_seen}
    selected = [item for item in candidates if id(item) in chosen_ids][:k]
    metadata = {
        "requested_k": k,
        "novel_count": sum(
            str(item.get("docid")) not in exclude_docids for item in selected
        ),
        "repeated_count": sum(
            str(item.get("docid")) in exclude_docids for item in selected
        ),
        "excluded_count": len(exclude_docids),
        "candidate_pool_exhausted": len(selected) < k,
        "exclusions_applied": True,
    }
    return selected, metadata


class BaseSearcher(ABC):
    """Abstract base class for all search implementations."""

    @classmethod
    @abstractmethod
    def parse_args(cls, parser: argparse.ArgumentParser) -> None:
        """Add searcher-specific arguments to the argument parser."""
        pass

    @abstractmethod
    def __init__(self, args):
        """Initialize the searcher with parsed arguments."""
        pass

    @abstractmethod
    def search(self, query: str, k: int = 10) -> List[Dict[str, Any]]:
        """
        Perform search and return results.

        Args:
            query: Search query string
            k: Number of results to return

        Returns:
            List of search results with format: {"docid": str, "score": float, "snippet": str}
        """
        pass

    def search_with_metadata(
        self,
        query: str,
        k: int = 10,
        *,
        exclude_docids: Optional[Iterable[str]] = None,
        seen_anchor_count: int = 0,
    ) -> Dict[str, Any]:
        """Search with optional per-request document novelty controls.

        Subclasses with access to their candidate pool should override this so
        exclusions can be applied before expensive reranking.  The default is a
        safe local fallback for existing searchers: it over-fetches by the number
        of exclusions, filters without changing relative rank, and may return
        fewer than ``k`` results when the underlying searcher cannot over-fetch.
        """

        k, excluded, seen_anchor_count = normalize_novelty_request(
            k, exclude_docids, seen_anchor_count
        )
        fetch_k = k + len(excluded)
        candidates = self.search(query, k=fetch_k)
        results, metadata = select_novelty_results(
            candidates,
            k=k,
            exclude_docids=excluded,
            seen_anchor_count=seen_anchor_count,
        )
        metadata["fallback_applied"] = bool(excluded)
        return {"results": results, "metadata": metadata}

    @abstractmethod
    def get_document(self, docid: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve full document by ID.

        Args:
            docid: Document ID to retrieve

        Returns:
            Document dictionary with format: {"docid": str, "snippet": str} or None if not found
        """
        pass

    @property
    @abstractmethod
    def search_type(self) -> str:
        """Return the type of search (e.g., 'BM25', 'FAISS')."""
        pass

    def search_description(self, k: int = 10) -> str:
        """
        Description of the search tool to be passed to the LLM.
        """
        return f"Perform a search on a knowledge source. Returns top-{k} hits with docid, score, and snippet. The snippet contains the document's contents (may be truncated based on token limits)."

    def get_document_description(self) -> str:
        """
        Description of the get_document tool to be passed to the LLM.
        """
        return "Retrieve a full document by its docid."
