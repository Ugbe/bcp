"""
Searchers package for different search implementations.
"""

import logging
from enum import Enum

from .base import BaseSearcher
from .custom_searcher import CustomSearcher
from .remote_api_searcher import RemoteApiSearcher

logger = logging.getLogger(__name__)

try:
    from .faiss_searcher import FaissSearcher, ReasonIrSearcher
except ImportError as e:  # faiss/tevatron/torch are not required by every searcher
    FaissSearcher = None
    ReasonIrSearcher = None
    logger.debug("faiss_searcher unavailable (optional dependency missing): %s", e)

try:
    from .bm25_searcher import BM25Searcher
except ImportError as e:  # pyserini requires Java and is not needed remotely
    BM25Searcher = None
    logger.debug("bm25_searcher unavailable (optional dependency missing): %s", e)


class SearcherType(Enum):
    """Enum for managing available searcher types and their CLI mappings."""

    BM25 = ("bm25", BM25Searcher)
    FAISS = ("faiss", FaissSearcher)
    REASONIR = ("reasonir", ReasonIrSearcher)
    CUSTOM = (
        "custom",
        CustomSearcher,
    )  # Your custom searcher class, yet to be implemented
    REMOTE = (
        "remote",
        RemoteApiSearcher,
    )  # HTTP proxy to the externally served hybrid retrieval API

    def __init__(self, cli_name, searcher_class):
        self.cli_name = cli_name
        self.searcher_class = searcher_class

    @classmethod
    def get_choices(cls):
        """Get list of CLI choices for argument parser."""
        return [
            searcher_type.cli_name
            for searcher_type in cls
            if searcher_type.searcher_class is not None
        ]

    @classmethod
    def get_searcher_class(cls, cli_name):
        """Get searcher class by CLI name."""
        for searcher_type in cls:
            if searcher_type.cli_name == cli_name:
                if searcher_type.searcher_class is None:
                    raise ValueError(
                        f"Searcher type '{cli_name}' is unavailable: optional "
                        "dependencies (faiss/tevatron/torch) are not installed."
                    )
                return searcher_type.searcher_class
        raise ValueError(f"Unknown searcher type: {cli_name}")


__all__ = ["BaseSearcher", "SearcherType"]
