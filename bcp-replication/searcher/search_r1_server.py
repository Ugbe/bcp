import argparse
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

import transformers
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from searchers import SearcherType

load_dotenv()

logger = logging.getLogger(__name__)

snippet_tokenizer = None

# Only the first snippet_max_tokens survive, so cut the raw string before tokenizing
# it. Corpus documents run to ~10 MB and encoding one in full to keep 512 tokens cost
# seconds per request, across all k results. 16 chars/token is a very safe
# overestimate for this tokenizer (natural text runs 3-4), so the snippet is
# unchanged -- only the work discarded to produce it is.
SNIPPET_CHAR_RATIO = 16
MAX_EXCLUDE_DOCIDS = 2000
MAX_EXCLUDE_DOCID_LENGTH = 256
MAX_RETRIEVAL_K = 100
MAX_QUERY_LENGTH = 16384


class SearchRequest(BaseModel):
    """Backward-compatible request body for both retrieval endpoints."""

    query: str
    exclude_docids: List[str] = Field(default_factory=list)
    k: Optional[int] = None
    seen_anchor_count: int = 0


def _normalize_search_request(
    request: SearchRequest, default_k: int
) -> Tuple[int, Set[str], int]:
    """Validate bounded novelty inputs and return their normalized form."""

    if not request.query or len(request.query) > MAX_QUERY_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=f"query must contain 1 to {MAX_QUERY_LENGTH} characters",
        )
    requested_k = default_k if request.k is None else request.k
    if isinstance(requested_k, bool) or not 1 <= requested_k <= MAX_RETRIEVAL_K:
        raise HTTPException(
            status_code=422,
            detail=f"k must be between 1 and {MAX_RETRIEVAL_K}",
        )
    if not 0 <= request.seen_anchor_count <= requested_k:
        raise HTTPException(
            status_code=422,
            detail="seen_anchor_count must be between 0 and k",
        )
    if len(request.exclude_docids) > MAX_EXCLUDE_DOCIDS:
        raise HTTPException(
            status_code=422,
            detail=f"exclude_docids may contain at most {MAX_EXCLUDE_DOCIDS} IDs",
        )
    if any(len(str(docid)) > MAX_EXCLUDE_DOCID_LENGTH for docid in request.exclude_docids):
        raise HTTPException(
            status_code=422,
            detail=(
                "each exclude_docids entry may contain at most "
                f"{MAX_EXCLUDE_DOCID_LENGTH} characters"
            ),
        )

    excluded = {str(docid) for docid in request.exclude_docids}
    return requested_k, excluded, request.seen_anchor_count


def _select_results(
    candidates: List[Dict[str, Any]],
    *,
    k: int,
    excluded: Set[str],
    seen_anchor_count: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Server-local fallback for searchers without candidate-stage filtering."""

    novel = [item for item in candidates if str(item.get("docid")) not in excluded]
    seen = [item for item in candidates if str(item.get("docid")) in excluded]
    novel_target = max(0, k - seen_anchor_count)
    chosen_novel = novel[:novel_target]
    chosen_seen = seen[:seen_anchor_count]
    remaining = k - len(chosen_novel) - len(chosen_seen)
    if remaining > 0:
        chosen_novel.extend(novel[len(chosen_novel) : len(chosen_novel) + remaining])
    selected_ids = {id(item) for item in chosen_novel + chosen_seen}
    selected = [item for item in candidates if id(item) in selected_ids][:k]
    metadata = {
        "requested_k": k,
        "novel_count": sum(str(item.get("docid")) not in excluded for item in selected),
        "repeated_count": sum(str(item.get("docid")) in excluded for item in selected),
        "excluded_count": len(excluded),
        "candidate_pool_exhausted": len(selected) < k,
        "exclusions_applied": True,
        "fallback_applied": True,
    }
    return selected, metadata


def _execute_search(searcher, request: SearchRequest, default_k: int) -> Dict[str, Any]:
    requested_k, excluded, seen_anchor_count = _normalize_search_request(
        request, default_k
    )
    if hasattr(searcher, "search_with_metadata"):
        response = searcher.search_with_metadata(
            request.query,
            k=requested_k,
            exclude_docids=excluded,
            seen_anchor_count=seen_anchor_count,
        )
        if isinstance(response, dict) and isinstance(response.get("results"), list):
            results = response["results"]
            metadata = dict(response.get("metadata") or {})
        else:
            raise HTTPException(
                status_code=500,
                detail="searcher returned an invalid novelty response",
            )
    else:
        # Compatibility path for an upstream searcher that has not implemented
        # candidate-stage exclusions.  It may return fewer than k after filtering.
        candidates = searcher.search(request.query, k=requested_k + len(excluded))
        results, metadata = _select_results(
            candidates,
            k=requested_k,
            excluded=excluded,
            seen_anchor_count=seen_anchor_count,
        )

    results = results[:requested_k]

    # Always advertise the upgraded server contract and normalize the mandatory
    # counts from the actual returned rows.
    metadata.update(
        {
            "requested_k": requested_k,
            "novel_count": sum(
                str(item.get("docid")) not in excluded for item in results
            ),
            "repeated_count": sum(
                str(item.get("docid")) in excluded for item in results
            ),
            "excluded_count": len(excluded),
            "candidate_pool_exhausted": len(results) < requested_k,
            "exclusions_applied": True,
        }
    )
    metadata.setdefault("fallback_applied", False)
    return {"results": results, "metadata": metadata}


def _snippet_head(text, snippet_max_tokens):
    return text[: snippet_max_tokens * SNIPPET_CHAR_RATIO]


def split_title_text(passage_text):
    """Split a raw corpus passage into (title, text)."""
    if passage_text.startswith("---\ntitle:"):
        title = passage_text.split("\n")[1].strip('"')[7:]
        text = "\n".join(passage_text.split("\n")[2:])
    else:
        content_lines = passage_text.split("\n")
        title = content_lines[0].strip('"') if content_lines else ""
        text = "\n".join(content_lines[1:]) if len(content_lines) > 1 else ""

    return title, text


def format_results_for_api(search_results, snippet_max_tokens=-1):
    """Transform searcher results to API format with optional snippet truncation."""
    global snippet_tokenizer

    formatted_results = []

    for result in search_results:
        title, text = split_title_text(result["text"])

        if snippet_max_tokens > 0 and snippet_tokenizer:
            try:
                tokens = snippet_tokenizer.encode(
                    _snippet_head(text, snippet_max_tokens), add_special_tokens=False
                )
                if len(tokens) > snippet_max_tokens:
                    truncated_tokens = tokens[:snippet_max_tokens]
                    text = snippet_tokenizer.decode(
                        truncated_tokens, skip_special_tokens=True
                    )
            except Exception as e:
                logger.warning(f"Failed to truncate snippet: {e}")

        formatted = {
            "document": {"title": title, "text": text},
            "docid": result["docid"],
        }
        if result.get("score") is not None:
            formatted["score"] = result["score"]
        formatted_results.append(formatted)

    return formatted_results


def truncate_snippet(text, snippet_max_tokens=-1):
    """Cap text at snippet_max_tokens, mirroring the MCP search tool's truncation."""
    global snippet_tokenizer

    if snippet_max_tokens > 0 and snippet_tokenizer:
        try:
            tokens = snippet_tokenizer.encode(
                _snippet_head(text, snippet_max_tokens), add_special_tokens=False
            )
            if len(tokens) > snippet_max_tokens:
                return snippet_tokenizer.decode(
                    tokens[:snippet_max_tokens], skip_special_tokens=True
                )
        except Exception as e:
            logger.warning(f"Failed to truncate snippet: {e}")

    return text


def format_results_for_tool(search_results, snippet_max_tokens=-1):
    """Transform searcher results into the same payload the MCP `search` tool returns."""
    formatted_results = []

    for result in search_results:
        snippet = truncate_snippet(result["text"], snippet_max_tokens)

        if result.get("score") is None:
            formatted_results.append({"docid": result["docid"], "snippet": snippet})
        else:
            formatted_results.append(
                {
                    "docid": result["docid"],
                    "score": result["score"],
                    "snippet": snippet,
                }
            )

    return formatted_results


def create_app(searcher, default_k: int, snippet_max_tokens: int) -> FastAPI:
    """Build the HTTP app separately so its contract can be tested offline."""

    app = FastAPI(title=f"{searcher.search_type} Search Server")

    @app.post("/retrieve")
    def search_endpoint(request: SearchRequest):
        search_response = _execute_search(searcher, request, default_k)
        formatted_results = format_results_for_api(
            search_response["results"], snippet_max_tokens
        )
        return {"result": formatted_results, "metadata": search_response["metadata"]}

    # The two endpoints below are the HTTP equivalents of the MCP tools in
    # searcher/tools.py.  /search intentionally keeps its legacy list response;
    # callers needing novelty diagnostics should use /retrieve.
    @app.post("/search")
    def search_tool_endpoint(request: SearchRequest):
        search_response = _execute_search(searcher, request, default_k)
        return format_results_for_tool(search_response["results"], snippet_max_tokens)

    @app.get("/get_document")
    def get_document_endpoint(docid: str):
        # Full, untruncated text -- served straight from the in-memory corpus map,
        # so no GPU work and no contention with the retrieval lock.
        document = searcher.get_document(docid)

        if document is None:
            raise HTTPException(status_code=404, detail=f"docid not found: {docid}")

        return document

    return app


def main():
    # Without this the root logger sits at WARNING and every logger.info in the
    # searcher -- index sizes, which checkpoints loaded, per-request stage timings --
    # is silently dropped, which makes the service impossible to diagnose from its log.
    # force=True because transformers installs a root handler on import, which makes
    # a plain basicConfig() a silent no-op.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    logging.getLogger("searchers").setLevel(logging.INFO)

    parser = argparse.ArgumentParser(
        description="Launch unified search server with BM25/FAISS/ReasonIR support"
    )

    parser.add_argument(
        "--searcher-type",
        choices=SearcherType.get_choices(),
        required=True,
        help=f"Type of searcher to use: {', '.join(SearcherType.get_choices())}",
    )

    # Server configuration arguments
    parser.add_argument(
        "--port", type=int, default=8001, help="Port for server (default: 8001)"
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Interface to bind. Use 127.0.0.1 to serve only via the authenticated proxy.",
    )
    parser.add_argument(
        "--snippet-max-tokens",
        type=int,
        default=512,
        help="Number of tokens to include for each document snippet in search results using Qwen/Qwen3-0.6B tokenizer (default: 512). Set to -1 to disable.",
    )
    parser.add_argument(
        "--k",
        type=int,
        default=5,
        help="Fixed number of search results to return for all queries in this session (default: 5).",
    )

    temp_args, _ = parser.parse_known_args()

    searcher_class = SearcherType.get_searcher_class(temp_args.searcher_type)

    searcher_class.parse_args(parser)

    args = parser.parse_args()

    global snippet_tokenizer
    if args.snippet_max_tokens > 0:
        print("Loading tokenizer for snippet truncation...")
        snippet_tokenizer = transformers.AutoTokenizer.from_pretrained(
            "Qwen/Qwen3-0.6B"
        )

    searcher = searcher_class(args)

    app = create_app(searcher, args.k, args.snippet_max_tokens)

    print(f"Starting {searcher.search_type} search server on {args.host}:{args.port}")
    print(
        f"Server configuration: k={args.k}, snippet_max_tokens={args.snippet_max_tokens}"
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
