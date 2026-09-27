"""Client-side Kev reranking: ordering, request contract, and runner wiring."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "search_agent"))
sys.path.insert(0, str(REPO_ROOT))

from chat_client import ChatSearchToolHandler, run_conversation_with_tools  # noqa: E402
from searcher.searchers.kev_reranker import KevReranker  # noqa: E402
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


class _FakeKev:
    """Scores a document by the number in its text; docs containing 'down' fail."""

    def __init__(self):
        self.headers = {}
        self.payloads = []

    def post(self, url, json=None, timeout=None):
        self.payloads.append({"url": url, **json})
        state = json["state"]
        if "down" in state:
            return _HttpResponse({"detail": "boom"}, status_code=503)
        prob = float(state.split("p=")[1].split()[0])
        return _HttpResponse({"answers": {"relevant": {"noul": prob}}})


class _DenseService:
    """The perfect retriever's /search: returns exactly k ranked hits."""

    def __init__(self, texts):
        self.texts = texts
        self.headers = {}
        self.requests = []

    def request(self, method, url, timeout=None, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        k = kwargs["json"]["k"]
        return _HttpResponse(
            {
                "results": [
                    {"docid": docid, "score": 1.0 - rank / 100, "text": text}
                    for rank, (docid, text) in enumerate(self.texts[:k])
                ]
            }
        )


TEXTS = [
    ("g1", "gold one p=0.2"),
    ("n1", "noise one p=0.9"),
    ("g2", "gold two p=0.7"),
    ("n2", "noise two p=0.7"),
    ("n3", "noise three p=0.1"),
    ("n4", "noise four p=0.99"),
]


def _kev(log_path=None, doc_chars=4096):
    kev = KevReranker("http://kev.invalid/", token="kt", doc_chars=doc_chars, log_path=log_path)
    kev.session = _FakeKev()
    return kev


def _searcher(api="dense", texts=TEXTS):
    searcher = RemoteApiSearcher(
        SimpleNamespace(
            retrieval_url="http://retrieval.invalid",
            retrieval_timeout=1.0,
            retrieval_retries=1,
            retrieval_retry_backoff=0.0,
            retrieval_token=None,
            retrieval_api=api,
            retrieval_model="browsecomp-overfit",
            retrieval_reasoning_max_chars=0,
            kev_url="http://kev.invalid",
            kev_token=None,
            kev_timeout=1.0,
            kev_doc_chars=4096,
            kev_log_path=None,
        )
    )
    searcher.session = _DenseService(texts)
    searcher.kev.session = _FakeKev()
    return searcher


def _hits(n=5):
    return [{"docid": d, "score": 1.0 - i / 100, "text": t} for i, (d, t) in enumerate(TEXTS[:n])]


class KevRerankerTests(unittest.TestCase):
    def test_orders_by_probability_and_breaks_ties_by_retriever_rank(self):
        reranked, meta = _kev().rerank("q", _hits())

        # g2 and n2 tie at 0.7; g2 was retrieved first.
        self.assertEqual([h["docid"] for h in reranked], ["n1", "g2", "n2", "g1", "n3"])
        self.assertEqual(meta["kev_requested"], 5)
        self.assertEqual(meta["kev_scored"], 5)

    def test_only_reorders_never_adds_or_drops(self):
        hits = _hits()
        reranked, _ = _kev().rerank("q", hits)
        self.assertEqual(len(reranked), len(hits))
        self.assertEqual({h["docid"] for h in reranked}, {h["docid"] for h in hits})

    def test_score_becomes_kev_probability_and_retriever_score_is_kept(self):
        reranked, _ = _kev().rerank("q", _hits(2))
        top = reranked[0]
        self.assertEqual(top["docid"], "n1")
        self.assertEqual(top["score"], 0.9)
        self.assertEqual(top["kev_prob"], 0.9)
        self.assertEqual(top["retriever_score"], 0.99)
        self.assertEqual(top["retriever_rank"], 2)

    def test_failed_document_keeps_retriever_score_behind_scored_ones(self):
        hits = [
            {"docid": "a", "score": 0.5, "text": "down"},
            {"docid": "b", "score": 0.4, "text": "p=0.1"},
        ]
        reranked, meta = _kev().rerank("q", hits)
        self.assertEqual([h["docid"] for h in reranked], ["b", "a"])
        self.assertEqual(reranked[1]["score"], 0.5)
        self.assertIsNone(reranked[1]["kev_prob"])
        self.assertEqual(meta["kev_scored"], 1)

    def test_request_contract_matches_the_agentir_service(self):
        real = KevReranker("http://kev.invalid/", token="kt")
        self.assertEqual(real.session.headers["Authorization"], "Bearer kt")
        kev = _kev(doc_chars=8)
        kev.rerank("Otto Knows", [{"docid": "a", "text": "p=0.5 and much more text"}])
        payload = kev.session.payloads[0]
        self.assertEqual(payload["url"], "http://kev.invalid/v1/systemone")
        self.assertEqual(payload["model"], "kev-latest")
        self.assertEqual(payload["state"], "p=0.5 an")
        question = payload["questions"]["relevant"]
        self.assertEqual(question["type"], "noul")
        self.assertIn("A researcher ran the web search query: Otto Knows", question["instructions"])

    def test_log_records_both_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "kev.jsonl"
            _kev(log_path=str(log)).rerank("q", _hits(3))
            record = json.loads(log.read_text(encoding="utf-8"))
        self.assertEqual([r["docid"] for r in record["retrieved"]], ["g1", "n1", "g2"])
        self.assertEqual(record["returned"], ["n1", "g2", "g1"])
        self.assertEqual([r["kev"] for r in record["retrieved"]], [0.2, 0.9, 0.7])


class SearcherWiringTests(unittest.TestCase):
    def test_search_returns_exactly_k_retrieved_docs_in_kev_order(self):
        searcher = _searcher()
        hits = searcher.search("q", k=5)
        self.assertEqual([h["docid"] for h in hits], ["n1", "g2", "n2", "g1", "n3"])
        self.assertEqual(searcher.session.requests[0]["json"]["k"], 5)
        # n4 ranked sixth, so Kev never sees it despite its high score.
        self.assertEqual(len(searcher.kev.session.payloads), 5)

    def test_novelty_selection_happens_before_kev(self):
        searcher = _searcher()
        response = searcher.search_with_metadata("q", k=5, exclude_docids={"g1"})
        docids = [h["docid"] for h in response["results"]]
        self.assertEqual(len(docids), 5)
        self.assertNotIn("g1", docids)
        self.assertEqual(docids, ["n4", "n1", "g2", "n2", "n3"])
        self.assertEqual(response["metadata"]["kev_scored"], 5)

    def test_multi_query_reranks_the_fused_selection_once(self):
        searcher = _searcher()
        response = searcher.search_multi_with_metadata(["q one", "q two"], k=5)
        self.assertEqual(len(response["results"]), 5)
        self.assertEqual(len(searcher.kev.session.payloads), 5)
        self.assertEqual(response["metadata"]["kev_scored"], 5)

    def test_agentir_service_refuses_a_second_kev(self):
        with self.assertRaises(ValueError):
            _searcher(api="agentir")


def _handler(searcher):
    handler = ChatSearchToolHandler.__new__(ChatSearchToolHandler)
    handler.searcher = searcher
    handler.tool_name = "search"
    handler.tool_param = "query"
    handler.include_get_document = True
    handler.k = 5
    handler.snippet_max_tokens = None
    handler.document_max_tokens = 100
    handler.tokenizer = None
    handler.multi_query_search = False
    handler.deep_pool_search = False
    handler.bulk_get_documents = False
    return handler


class _Message:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self, mode="python"):
        return {"role": "assistant", "content": self.content, "tool_calls": self.tool_calls}


def _completion(content, tool_calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=_Message(content, tool_calls), finish_reason="stop")],
        usage=None,
    )


class ModelSeesKevOrderTests(unittest.TestCase):
    def _tool_message(self, retrieval_novelty):
        searcher = _searcher()
        responses = iter(
            [
                _completion(
                    None,
                    tool_calls=[
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "search", "arguments": json.dumps({"query": "q"})},
                        }
                    ],
                ),
                _completion("Explanation: [g2]\nExact Answer: x\nConfidence: 80%"),
            ]
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: next(responses)))
        )
        messages, _, status = run_conversation_with_tools(
            client, "test", [{"role": "user", "content": "Who?"}], [],
            _handler(searcher), max_iterations=4, max_tool_calls=4,
            retrieval_novelty=retrieval_novelty,
        )
        self.assertEqual(status, "completed")
        tool = next(m for m in messages if m.get("role") == "tool")
        return json.loads(tool["content"])

    def test_plain_search(self):
        shown = self._tool_message("off")
        self.assertEqual([h["docid"] for h in shown], ["n1", "g2", "n2", "g1", "n3"])
        self.assertEqual([h["score"] for h in shown], [0.9, 0.7, 0.7, 0.2, 0.1])
        # Kev bookkeeping stays out of the model's context.
        self.assertEqual(set(shown[0]), {"docid", "snippet", "score"})

    def test_novelty_search(self):
        shown = self._tool_message("server")
        self.assertEqual([h["docid"] for h in shown["documents"]], ["n1", "g2", "n2", "g1", "n3"])
        self.assertFalse([key for key in shown["retrieval_state"] if key.startswith("kev_")])


if __name__ == "__main__":
    unittest.main()
