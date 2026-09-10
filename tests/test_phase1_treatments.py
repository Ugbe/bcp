import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "search_agent"))
sys.path.insert(0, str(ROOT))

from chat_client import ChatSearchToolHandler, run_conversation_with_tools  # noqa: E402
from research_ensemble import decide_ensemble  # noqa: E402
from searcher.searchers.remote_api_searcher import RemoteApiSearcher  # noqa: E402


class _Message:
    def __init__(self, content):
        self.content = content
        self.tool_calls = None

    def model_dump(self, mode="python"):
        return {"role": "assistant", "content": self.content, "tool_calls": None}


class _Choice:
    finish_reason = "stop"

    def __init__(self, content):
        self.message = _Message(content)


class _Response:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class _Completions:
    def __init__(self):
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return _Response("Explanation: evidence\nExact Answer: Ada\nConfidence: 80%")


class _Client:
    def __init__(self):
        self.chat = type("Chat", (), {})()
        self.chat.completions = _Completions()


class _Handler:
    def execute_tool(self, name, arguments):
        raise AssertionError("no tool call expected")


class Phase1TreatmentTests(unittest.TestCase):
    def test_seed_is_forwarded_to_model_request(self):
        client = _Client()
        run_conversation_with_tools(
            client,
            "model",
            [{"role": "user", "content": "question"}],
            [],
            _Handler(),
            max_iterations=1,
            max_tokens=32,
            require_final_answer=True,
            seed=1234,
        )
        self.assertEqual(client.chat.completions.requests[0]["seed"], 1234)

    def test_rrf_fuses_query_rankings(self):
        fused = RemoteApiSearcher.reciprocal_rank_fusion(
            [
                [{"docid": "a", "text": "A"}, {"docid": "b", "text": "B"}],
                [{"docid": "b", "text": "B"}, {"docid": "c", "text": "C"}],
            ],
            k=3,
        )
        self.assertEqual([item["docid"] for item in fused], ["b", "a", "c"])
        self.assertGreater(fused[0]["score"], fused[1]["score"])

    def test_ensemble_pooled_final_has_double_vote(self):
        decision = decide_ensemble(
            [
                {"status": "completed", "final_answer": "Ada", "confidence": 80, "rollout_index": 1},
                {"status": "completed", "final_answer": "Ada", "confidence": 70, "rollout_index": 2},
                {"status": "completed", "final_answer": "Grace", "confidence": 90, "rollout_index": 3},
            ],
            {"status": "completed", "final_answer": "Grace", "confidence": 95},
        )
        self.assertEqual(decision["selected"]["answer"], "Grace")
        self.assertEqual(decision["votes"]["grace"]["weight"], 3)

    def test_handler_schema_exposes_opt_in_retrieval_tools(self):
        handler = object.__new__(ChatSearchToolHandler)
        handler.tool_name = "search"
        handler.tool_param = "query"
        handler.multi_query_search = True
        handler.deep_pool_search = True
        handler.deep_pool_k = 100
        handler.include_get_document = False
        handler.k = 10
        handler.searcher = type(
            "Searcher",
            (),
            {
                "search_description": lambda self, k: "search",
                "get_document_description": lambda self: "document",
            },
        )()
        tools = handler.get_chat_tool_definitions()
        names = [tool["function"]["name"] for tool in tools]
        self.assertEqual(names, ["search", "deep_search"])
        schema = tools[0]["function"]["parameters"]["properties"]["query"]
        self.assertEqual(schema["anyOf"][1]["maxItems"], 4)


if __name__ == "__main__":
    unittest.main()
