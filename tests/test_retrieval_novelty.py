import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
from fastapi.testclient import TestClient


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from searcher.searchers.base import (  # noqa: E402
    MAX_EXCLUDE_DOCIDS,
    BaseSearcher,
)
from searcher.searchers.remote_api_searcher import RemoteApiSearcher  # noqa: E402


class _ListSearcher(BaseSearcher):
    @classmethod
    def parse_args(cls, parser):
        return None

    def __init__(self, rows, hard_limit=None):
        self.rows = rows
        self.hard_limit = hard_limit
        self.requested_ks = []

    def search(self, query, k=10):
        self.requested_ks.append(k)
        if self.hard_limit is not None:
            k = min(k, self.hard_limit)
        return self.rows[:k]

    def get_document(self, docid):
        for row in self.rows:
            if str(row["docid"]) == str(docid):
                return row
        return None

    @property
    def search_type(self):
        return "fake"


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.reason = "test"
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} test")


class _FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []
        self.headers = {}

    def request(self, method, url, timeout=None, **kwargs):
        self.requests.append(
            {"method": method, "url": url, "timeout": timeout, **kwargs}
        )
        return next(self.responses)


def _remote_searcher(responses, token=None, retrieval_api="legacy", retrieval_model=None):
    args = SimpleNamespace(
        retrieval_url="http://retrieval.invalid",
        retrieval_timeout=1.0,
        retrieval_retries=1,
        retrieval_retry_backoff=0.0,
        retrieval_token=token,
        retrieval_api=retrieval_api,
        retrieval_model=retrieval_model,
    )
    searcher = RemoteApiSearcher(args)
    searcher.session = _FakeSession(responses)
    if token:
        searcher.session.headers["Authorization"] = f"Bearer {token}"
    return searcher


def _load_server_module():
    """Load the deployment server without importing its GPU search package."""

    module_name = "_test_bcp_search_r1_server"
    path = REPO_ROOT / "bcp-replication" / "searcher" / "search_r1_server.py"
    fake_searchers = types.ModuleType("searchers")
    fake_searchers.SearcherType = type("SearcherType", (), {})
    with patch.dict(sys.modules, {"searchers": fake_searchers}):
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
    return module


def _load_hybrid_class():
    """Load HybridSearcher for pure selection tests without GPU dependencies."""

    package_name = "_test_bcp_searchers"
    module_name = f"{package_name}.hybrid_searcher"
    path = (
        REPO_ROOT
        / "bcp-replication"
        / "searcher"
        / "searchers"
        / "hybrid_searcher.py"
    )

    package = types.ModuleType(package_name)
    package.__path__ = []
    base = types.ModuleType(f"{package_name}.base")
    base.BaseSearcher = object
    torch = types.ModuleType("torch")
    torch.no_grad = lambda: (lambda function: function)
    torch.Tensor = object
    torch.OutOfMemoryError = RuntimeError
    datasets = types.ModuleType("datasets")
    datasets.load_dataset = lambda *args, **kwargs: None
    peft = types.ModuleType("peft")
    peft.PeftModel = object
    transformers = types.ModuleType("transformers")
    transformers.AutoModel = object
    transformers.AutoModelForCausalLM = object
    transformers.AutoTokenizer = object

    stubs = {
        package_name: package,
        f"{package_name}.base": base,
        "faiss": types.ModuleType("faiss"),
        "torch": torch,
        "datasets": datasets,
        "peft": peft,
        "transformers": transformers,
    }
    with patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
    return module.HybridSearcher


class BaseNoveltyTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {"docid": str(index), "score": 10 - index, "text": str(index)}
            for index in range(8)
        ]

    def test_default_interface_overfetches_and_excludes_stably(self):
        searcher = _ListSearcher(self.rows)
        response = searcher.search_with_metadata(
            "q", k=3, exclude_docids=["1", "3"]
        )

        self.assertEqual(searcher.requested_ks, [5])
        self.assertEqual([row["docid"] for row in response["results"]], ["0", "2", "4"])
        self.assertEqual(response["metadata"]["novel_count"], 3)
        self.assertEqual(response["metadata"]["repeated_count"], 0)
        self.assertFalse(response["metadata"]["candidate_pool_exhausted"])

    def test_optional_anchors_preserve_original_ranking(self):
        searcher = _ListSearcher(self.rows)
        response = searcher.search_with_metadata(
            "q", k=4, exclude_docids=["1", "3"], seen_anchor_count=2
        )

        self.assertEqual([row["docid"] for row in response["results"]], ["0", "1", "2", "3"])
        self.assertEqual(response["metadata"]["novel_count"], 2)
        self.assertEqual(response["metadata"]["repeated_count"], 2)

    def test_exhausted_fallback_returns_fewer_than_k_and_says_so(self):
        searcher = _ListSearcher(self.rows, hard_limit=3)
        response = searcher.search_with_metadata(
            "q", k=3, exclude_docids=["0", "2"]
        )

        self.assertEqual([row["docid"] for row in response["results"]], ["1"])
        self.assertTrue(response["metadata"]["candidate_pool_exhausted"])

    def test_exclusion_bound_and_anchor_validation(self):
        searcher = _ListSearcher(self.rows)
        with self.assertRaisesRegex(ValueError, "at most"):
            searcher.search_with_metadata(
                "q", exclude_docids=map(str, range(MAX_EXCLUDE_DOCIDS + 1))
            )
        with self.assertRaisesRegex(ValueError, "between 0 and k"):
            searcher.search_with_metadata("q", k=2, seen_anchor_count=3)


class RemoteApiNoveltyTests(unittest.TestCase):
    def test_legacy_search_request_and_response_are_unchanged(self):
        searcher = _remote_searcher(
            [_FakeResponse({"result": [{"docid": 7, "document": {"text": "hit"}}]})]
        )

        results = searcher.search("legacy", k=1)

        self.assertEqual(results, [{"docid": "7", "score": None, "text": "hit"}])
        self.assertEqual(searcher.session.requests[0]["json"], {"query": "legacy"})

    def test_upgraded_server_metadata_and_request_fields(self):
        payload = {
            "result": [
                {"docid": "2", "score": 0.8, "document": {"text": "new"}},
                {"docid": "1", "score": 0.7, "document": {"text": "anchor"}},
            ],
            "metadata": {"exclusions_applied": True, "server_detail": "kept"},
        }
        searcher = _remote_searcher([_FakeResponse(payload)])

        response = searcher.search_with_metadata(
            "new", k=2, exclude_docids=["1"], seen_anchor_count=1
        )

        self.assertEqual(
            searcher.session.requests[0]["json"],
            {
                "query": "new",
                "exclude_docids": ["1"],
                "k": 2,
                "seen_anchor_count": 1,
            },
        )
        self.assertEqual([row["docid"] for row in response["results"]], ["2", "1"])
        self.assertEqual(response["metadata"]["server_detail"], "kept")
        self.assertFalse(response["metadata"]["fallback_applied"])
        self.assertEqual(response["metadata"]["repeated_count"], 1)

    def test_missing_metadata_uses_local_fallback_and_reports_exhaustion(self):
        payload = {
            "result": [
                {"docid": "seen", "document": {"text": "old"}},
                {"docid": "new", "document": {"text": "new"}},
            ]
        }
        searcher = _remote_searcher([_FakeResponse(payload)])

        response = searcher.search_with_metadata(
            "q", k=2, exclude_docids=["seen"]
        )

        self.assertEqual([row["docid"] for row in response["results"]], ["new"])
        self.assertTrue(response["metadata"]["fallback_applied"])
        self.assertTrue(response["metadata"]["candidate_pool_exhausted"])
        self.assertTrue(response["metadata"]["exclusions_applied"])

    def test_schema_rejection_retries_legacy_shape_once(self):
        searcher = _remote_searcher(
            [
                _FakeResponse({"detail": "extra forbidden"}, status_code=422),
                _FakeResponse({"result": [{"docid": "9", "document": {"text": "x"}}]}),
            ]
        )

        response = searcher.search_with_metadata("q", k=1, exclude_docids=["8"])

        self.assertEqual(len(searcher.session.requests), 2)
        self.assertEqual(searcher.session.requests[1]["json"], {"query": "q"})
        self.assertTrue(response["metadata"]["legacy_request_retried"])

    def test_authentication_header_is_unchanged(self):
        searcher = _remote_searcher([], token="secret")
        self.assertEqual(searcher.session.headers["Authorization"], "Bearer secret")

    def test_dense_search_uses_new_path_response_shape_and_encoder(self):
        searcher = _remote_searcher(
            [_FakeResponse({"results": [{"docid": 7, "score": 0.9, "text": "hit"}]})],
            retrieval_api="dense",
            retrieval_model="browsecomp-overfit",
        )

        results = searcher.search("dense", k=3)

        self.assertEqual(results, [{"docid": "7", "score": 0.9, "text": "hit"}])
        request = searcher.session.requests[0]
        self.assertEqual(request["url"], "http://retrieval.invalid/search")
        self.assertEqual(
            request["json"],
            {
                "query": "dense",
                "k": 3,
                "include_text": True,
                "model": "browsecomp-overfit",
            },
        )

    def test_dense_bulk_documents_uses_one_request_and_preserves_order(self):
        searcher = _remote_searcher(
            [
                _FakeResponse(
                    {
                        "documents": [
                            {"docid": "b", "text": "B"},
                            {"docid": "a", "text": "A"},
                        ]
                    }
                )
            ],
            retrieval_api="dense",
        )

        documents = searcher.get_documents(["a", "b", "a"])

        self.assertEqual(
            documents,
            [{"docid": "a", "text": "A"}, {"docid": "b", "text": "B"}],
        )
        request = searcher.session.requests[0]
        self.assertEqual(request["url"], "http://retrieval.invalid/documents")
        self.assertEqual(request["json"], {"docids": ["a", "b"]})


class RetrievalServerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _load_server_module()

    def test_legacy_request_uses_default_k_and_returns_wrapped_result(self):
        rows = [
            {"docid": str(index), "score": 1.0 - index / 10, "text": f"T{index}\nbody"}
            for index in range(4)
        ]
        app = self.server.create_app(_ListSearcher(rows), 3, -1)

        response = TestClient(app).post("/retrieve", json={"query": "legacy"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["result"]), 3)
        self.assertEqual(payload["metadata"]["requested_k"], 3)
        self.assertTrue(payload["metadata"]["exclusions_applied"])

    def test_server_fallback_honors_exclusions_k_and_anchors(self):
        rows = [
            {"docid": str(index), "score": 1.0, "text": f"T{index}\nbody"}
            for index in range(6)
        ]
        app = self.server.create_app(_ListSearcher(rows), 3, -1)

        response = TestClient(app).post(
            "/retrieve",
            json={
                "query": "q",
                "exclude_docids": ["1", "3"],
                "k": 4,
                "seen_anchor_count": 1,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([row["docid"] for row in payload["result"]], ["0", "1", "2", "4"])
        self.assertEqual(payload["metadata"]["novel_count"], 3)
        self.assertEqual(payload["metadata"]["repeated_count"], 1)

    def test_server_rejects_unbounded_exclusion_list(self):
        app = self.server.create_app(_ListSearcher([]), 3, -1)
        response = TestClient(app).post(
            "/retrieve",
            json={
                "query": "q",
                "exclude_docids": list(map(str, range(self.server.MAX_EXCLUDE_DOCIDS + 1))),
            },
        )
        self.assertEqual(response.status_code, 422)

    def test_get_document_remains_independent_of_seen_state(self):
        rows = [{"docid": "seen", "score": 1.0, "text": "Title\nfull text"}]
        app = self.server.create_app(_ListSearcher(rows), 1, -1)
        client = TestClient(app)
        client.post(
            "/retrieve",
            json={"query": "q", "exclude_docids": ["seen"]},
        )

        response = client.get("/get_document", params={"docid": "seen"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["docid"], "seen")


class HybridSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hybrid = _load_hybrid_class()

    def test_hard_exclusion_and_anchor_selection_are_stable(self):
        ranked = [(str(index), 10.0 - index) for index in range(6)]
        hard = self.hybrid._select_ranked_with_novelty(
            ranked, k=3, excluded={"1", "3"}, seen_anchor_count=0
        )
        anchored = self.hybrid._select_ranked_with_novelty(
            ranked, k=4, excluded={"1", "3"}, seen_anchor_count=2
        )

        self.assertEqual([docid for docid, _ in hard], ["0", "2", "4"])
        self.assertEqual([docid for docid, _ in anchored], ["0", "1", "2", "3"])

    def test_exclusions_are_filtered_before_reranking_and_legs_overfetch(self):
        class _ImmediateFuture:
            def __init__(self, value):
                self.value = value

            def result(self):
                return self.value

        class _ImmediatePool:
            def submit(self, function, *args):
                return _ImmediateFuture(function(*args))

        class _Lock:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

        searcher = self.hybrid.__new__(self.hybrid)
        searcher.args = SimpleNamespace(
            candidates_k=3,
            rerank_depth=3,
            disable_bm25=False,
            disable_dense=True,
            disable_rerank=False,
            rrf_k=60,
        )
        searcher.docid_to_text = {str(i): f"text {i}" for i in range(6)}
        searcher._cpu_pool = _ImmediatePool()
        searcher.gpu_lock = _Lock()
        leg_ks = []
        reranked_docids = []

        def bm25_search(query, k):
            leg_ks.append(k)
            return [str(i) for i in range(min(k, 6))]

        def rerank(query, docids):
            reranked_docids.extend(docids)
            return [(docid, 1.0 - index / 10) for index, docid in enumerate(docids)]

        searcher._bm25_search = bm25_search
        searcher._rerank = rerank

        response = searcher.search_with_metadata(
            "q", k=3, exclude_docids=["1", "2"]
        )

        self.assertEqual(leg_ks, [5])
        self.assertEqual(reranked_docids, ["0", "3", "4"])
        self.assertEqual([row["docid"] for row in response["results"]], ["0", "3", "4"])
        self.assertEqual(response["metadata"]["overfetch_k"], 5)


if __name__ == "__main__":
    unittest.main()
