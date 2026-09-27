"""AgentIR-4B + Kev retrieval API: request contract and reasoning passthrough."""

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "search_agent"))
sys.path.insert(0, str(REPO_ROOT))

from chat_client import ChatSearchToolHandler, run_conversation_with_tools  # noqa: E402
from searcher.searchers.remote_api_searcher import RemoteApiSearcher  # noqa: E402


class _HttpResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.reason = "test"
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} test")


class _AgentIrService:
    """Answers the AgentIR+Kev routes and records every request."""

    def __init__(self, docids=("d1", "d2", "d3")):
        self.docids = list(docids)
        self.requests = []
        self.headers = {}

    def request(self, method, url, timeout=None, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        path = url.split("retrieval.invalid", 1)[1]
        if method == "POST" and path == "/search":
            k = kwargs["json"]["k"]
            results = [
                {"docid": docid, "score": 0.9, "text": f"text {docid}", "kev_prob": 0.9}
                for docid in self.docids[:k]
            ]
            return _HttpResponse(
                {"model": "agentir-4b+kev", "k": k, "candidates": 50,
                 "kev_scored": 50, "results": results}
            )
        if method == "GET" and path.startswith("/document/"):
            docid = path.rsplit("/", 1)[1]
            return _HttpResponse({"docid": docid, "text": f"full {docid}"})
        return _HttpResponse({"detail": "Not Found"}, status_code=404)


def _searcher(service, reasoning_max_chars=12000, candidates=0):
    searcher = RemoteApiSearcher(
        SimpleNamespace(
            retrieval_url="http://retrieval.invalid",
            retrieval_timeout=1.0,
            retrieval_retries=1,
            retrieval_retry_backoff=0.0,
            retrieval_token=None,
            retrieval_api="agentir",
            retrieval_model="ignored-by-agentir",
            retrieval_reasoning_max_chars=reasoning_max_chars,
            retrieval_candidates=candidates,
        )
    )
    searcher.session = service
    return searcher


def _handler(searcher):
    handler = ChatSearchToolHandler.__new__(ChatSearchToolHandler)
    handler.searcher = searcher
    handler.tool_name = "search"
    handler.tool_param = "query"
    handler.include_get_document = True
    handler.k = 10
    handler.snippet_max_tokens = None
    handler.document_max_tokens = 100
    handler.tokenizer = None
    handler.multi_query_search = False
    handler.deep_pool_search = False
    handler.bulk_get_documents = False
    return handler


class _Message:
    def __init__(self, content, tool_calls, reasoning):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning = reasoning

    def model_dump(self, mode="python"):
        return {
            "role": "assistant",
            "content": self.content,
            "tool_calls": self.tool_calls,
            "reasoning": self.reasoning,
        }


class _Completion:
    def __init__(self, content, tool_calls=None, reasoning=None):
        choice = SimpleNamespace(
            message=_Message(content, tool_calls, reasoning), finish_reason="stop"
        )
        self.choices = [choice]
        self.usage = None


class _Client:
    def __init__(self, completions):
        responses = iter(completions)
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kwargs: next(responses))
        )


def _search_call(query):
    return [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "search", "arguments": json.dumps({"query": query})},
        }
    ]


class AgentIrSearcherTests(unittest.TestCase):
    def test_search_sends_reasoning_tail_and_no_encoder_name(self):
        service = _AgentIrService()
        searcher = _searcher(service, reasoning_max_chars=5)

        results = searcher.search("Otto Knows", k=2, reasoning="  abcdefghij  ")

        self.assertEqual([hit["docid"] for hit in results], ["d1", "d2"])
        request = service.requests[0]
        self.assertEqual(request["url"], "http://retrieval.invalid/search")
        self.assertEqual(
            request["json"],
            {"query": "Otto Knows", "k": 2, "include_text": True, "reasoning": "fghij"},
        )

    def test_pinned_candidate_count_is_sent_on_every_search_path(self):
        service = _AgentIrService()
        searcher = _searcher(service, candidates=200)
        searcher.search("q", k=1)
        searcher.search_with_metadata("q", k=1, exclude_docids={"x"})
        self.assertEqual([r["json"]["candidates"] for r in service.requests], [200, 200])

    def test_unpinned_candidate_count_is_left_to_the_server(self):
        service = _AgentIrService()
        _searcher(service).search("q", k=1)
        self.assertNotIn("candidates", service.requests[0]["json"])

    def test_missing_reasoning_is_omitted(self):
        service = _AgentIrService()
        _searcher(service).search("q", k=1)
        self.assertNotIn("reasoning", service.requests[0]["json"])

    def test_zero_budget_disables_reasoning(self):
        service = _AgentIrService()
        searcher = _searcher(service, reasoning_max_chars=0)
        self.assertFalse(searcher.accepts_reasoning)
        searcher.search("q", k=1, reasoning="thoughts")
        self.assertNotIn("reasoning", service.requests[0]["json"])

    def test_novelty_filters_top_k_without_overfetching(self):
        # Over-fetching would make the server run one Kev call per extra doc.
        service = _AgentIrService()
        searcher = _searcher(service)

        response = searcher.search_with_metadata(
            "q", k=3, exclude_docids={"d1", "x", "y", "z"}, reasoning="why"
        )

        self.assertEqual(service.requests[0]["json"]["k"], 3)
        self.assertEqual(service.requests[0]["json"]["reasoning"], "why")
        self.assertEqual([hit["docid"] for hit in response["results"]], ["d2", "d3"])
        self.assertEqual(response["metadata"]["retrieval_api"], "agentir")
        self.assertFalse(response["metadata"]["server_exclusions_applied"])

    def test_documents_use_single_document_route(self):
        service = _AgentIrService()
        searcher = _searcher(service)

        self.assertEqual(searcher.get_document("d1"), {"docid": "d1", "text": "full d1"})
        self.assertEqual(
            searcher.get_documents(["d2", "d1", "d2"]),
            [{"docid": "d2", "text": "full d2"}, {"docid": "d1", "text": "full d1"}],
        )
        self.assertEqual(
            [request["url"] for request in service.requests],
            [
                "http://retrieval.invalid/document/d1",
                "http://retrieval.invalid/document/d2",
                "http://retrieval.invalid/document/d1",
            ],
        )


class ReasoningPassthroughTests(unittest.TestCase):
    """The agent's reasoning for a search turn must reach the retrieval request."""

    def _run(self, retrieval_novelty):
        service = _AgentIrService()
        client = _Client(
            [
                _Completion(
                    None,
                    tool_calls=_search_call("Swedish progressive house DJ"),
                    reasoning="The clue points to a Swedish producer.",
                ),
                _Completion("Explanation: [d1]\nExact Answer: Otto Knows\nConfidence: 80%"),
            ]
        )
        _, usage, status = run_conversation_with_tools(
            client, "test", [{"role": "user", "content": "Who?"}], [],
            _handler(_searcher(service)), max_iterations=4, max_tool_calls=4,
            retrieval_novelty=retrieval_novelty,
        )
        self.assertEqual(status, "completed")
        self.assertEqual(usage, {"search": 1})
        return service.requests[0]["json"]

    def test_plain_search_carries_turn_reasoning(self):
        payload = self._run("off")
        self.assertEqual(payload["reasoning"], "The clue points to a Swedish producer.")
        self.assertEqual(payload["query"], "Swedish progressive house DJ")

    def test_novelty_search_carries_turn_reasoning(self):
        payload = self._run("server")
        self.assertEqual(payload["reasoning"], "The clue points to a Swedish producer.")
        self.assertEqual(payload["k"], 10)


if __name__ == "__main__":
    unittest.main()
