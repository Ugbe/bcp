"""Neutron dense retrieval service: search, API contract, auth, and runner compatibility."""

import importlib.util
import pickle
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

spec = importlib.util.spec_from_file_location(
    "neutron_server", REPO_ROOT / "neutron-retrieval" / "neutron_server.py"
)
neutron = importlib.util.module_from_spec(spec)
spec.loader.exec_module(neutron)

from searcher.searchers.remote_api_searcher import RemoteApiSearcher  # noqa: E402

DIM = 4
LOOKUP = ["d0", "d1", "d2", "d3", "d4", "d5"]
REPS = np.eye(len(LOOKUP), DIM, dtype=np.float32)
REPS[4] = np.array([0.6, 0.8, 0, 0], dtype=np.float32)
REPS[5] = np.array([0.8, 0.6, 0, 0], dtype=np.float32)
DOCS = {docid: f"text of {docid}" for docid in LOOKUP}


def fake_encode(query, name):
    """Overfit points at d1; base points at d0. Records every call."""
    fake_encode.calls.append((query, name))
    vec = [0, 1, 0, 0] if name == neutron.OVERFIT_NAME else [1, 0, 0, 0]
    return np.array(vec, dtype=np.float32)


fake_encode.calls = []


def make_retriever():
    return neutron.Retriever(
        REPS, LOOKUP, DOCS, fake_encode,
        [neutron.OVERFIT_NAME, neutron.BASE_MODEL], neutron.OVERFIT_NAME,
    )


class RetrieverTests(unittest.TestCase):
    def test_exact_top_k_by_inner_product(self):
        hits = make_retriever().search("q", 3, None)
        self.assertEqual([h["docid"] for h in hits], ["d1", "d4", "d5"])
        self.assertAlmostEqual(hits[1]["score"], 0.8, places=6)

    def test_encoder_selection(self):
        hits = make_retriever().search("q", 1, neutron.BASE_MODEL)
        self.assertEqual(hits[0]["docid"], "d0")
        self.assertEqual(fake_encode.calls[-1], ("q", neutron.BASE_MODEL))

    def test_ties_break_by_index_position(self):
        tied = np.ones((3, DIM), dtype=np.float32) / 2
        r = neutron.Retriever(tied, ["a", "b", "c"], {}, lambda q, n: np.ones(DIM) / 2,
                              [neutron.OVERFIT_NAME], neutron.OVERFIT_NAME)
        self.assertEqual([h["docid"] for h in r.search("q", 3, None)], ["a", "b", "c"])

    def test_k_is_clamped_to_corpus_size(self):
        self.assertEqual(len(make_retriever().search("q", 50, None)), len(LOOKUP))

    def test_unknown_encoder_raises(self):
        with self.assertRaises(KeyError):
            make_retriever().search("q", 1, "nope")


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(neutron.build_app(make_retriever(), api_token="secret"))
        self.auth = {"Authorization": "Bearer secret"}

    def test_search_contract(self):
        r = self.client.post("/search", json={"query": "q", "k": 2}, headers=self.auth)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["model"], neutron.OVERFIT_NAME)
        self.assertEqual(body["k"], 2)
        self.assertEqual(body["results"][0], {"docid": "d1", "score": 1.0, "text": "text of d1"})

    def test_search_without_text_and_with_base_model(self):
        r = self.client.post(
            "/search",
            json={"query": "q", "k": 1, "include_text": False, "model": neutron.BASE_MODEL},
            headers=self.auth,
        )
        self.assertEqual(r.json()["results"], [{"docid": "d0", "score": 1.0}])

    def test_unknown_model_and_empty_query_are_400(self):
        r = self.client.post("/search", json={"query": "q", "model": "nope"}, headers=self.auth)
        self.assertEqual(r.status_code, 400)
        self.assertIn("browsecomp-overfit", r.json()["detail"])
        r = self.client.post("/search", json={"query": "  "}, headers=self.auth)
        self.assertEqual(r.status_code, 400)

    def test_documents(self):
        self.assertEqual(
            self.client.get("/document/d3", headers=self.auth).json(),
            {"docid": "d3", "text": "text of d3"},
        )
        self.assertEqual(self.client.get("/document/zz", headers=self.auth).status_code, 404)
        r = self.client.post("/documents", json={"docids": ["d2", "zz", "d0", "d2"]}, headers=self.auth)
        self.assertEqual([d["docid"] for d in r.json()["documents"]], ["d2", "d0"])

    def test_auth(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.post("/search", json={"query": "q"}).status_code, 401)
        self.assertEqual(self.client.get("/info").status_code, 401)
        wrong = {"Authorization": "Bearer nope"}
        self.assertEqual(self.client.get("/document/d1", headers=wrong).status_code, 401)
        info = self.client.get("/info", headers=self.auth).json()
        self.assertEqual(info["docs"], len(LOOKUP))
        self.assertIn("upper bound", info["oracle_warning"])


class IndexLoadingTests(unittest.TestCase):
    def test_merges_shards_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i, part in enumerate((slice(0, 3), slice(3, 6))):
                with open(Path(tmp) / f"corpus.shard.{i}.pkl", "wb") as f:
                    pickle.dump((REPS[part] / np.linalg.norm(REPS[part], axis=1, keepdims=True), LOOKUP[part]), f)
            matrix, lookup = neutron.load_index(tmp)
        self.assertEqual(matrix.shape, (6, DIM))
        self.assertEqual(lookup, LOOKUP)

    def test_rejects_unnormalized_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(Path(tmp) / "corpus.pkl", "wb") as f:
                pickle.dump((REPS * 3, LOOKUP), f)
            with self.assertRaises(SystemExit):
                neutron.load_index(tmp)


class _TestClientSession:
    """requests.Session stand-in that routes the runner's calls into the app."""

    def __init__(self, client):
        self.client = client
        self.headers = {}

    def request(self, method, url, timeout=None, **kwargs):
        path = url.split("retrieval.invalid", 1)[1]
        return self.client.request(method, path, headers=self.headers, **kwargs)


class RunnerCompatibilityTests(unittest.TestCase):
    """The runner's dense client must work against this server unchanged."""

    def setUp(self):
        client = TestClient(neutron.build_app(make_retriever(), api_token="secret"))
        self.searcher = RemoteApiSearcher(
            SimpleNamespace(
                retrieval_url="http://retrieval.invalid",
                retrieval_timeout=5.0,
                retrieval_retries=1,
                retrieval_retry_backoff=0.0,
                retrieval_token="secret",
                retrieval_api="dense",
                retrieval_model=neutron.OVERFIT_NAME,
                retrieval_reasoning_max_chars=0,
            )
        )
        session = _TestClientSession(client)
        session.headers.update(self.searcher.session.headers)
        self.searcher.session = session

    def test_search_get_document_and_batch(self):
        hits = self.searcher.search("q", k=5)
        self.assertEqual([h["docid"] for h in hits], ["d1", "d4", "d5", "d0", "d2"])
        self.assertEqual(hits[0]["text"], "text of d1")
        self.assertEqual(self.searcher.get_document("d4"), {"docid": "d4", "text": "text of d4"})
        self.assertEqual(
            [d["docid"] for d in self.searcher.get_documents(["d5", "d1"])], ["d5", "d1"]
        )

    def test_local_novelty_over_dense(self):
        response = self.searcher.search_with_metadata("q", k=5, exclude_docids={"d1"})
        self.assertEqual([h["docid"] for h in response["results"]], ["d4", "d5", "d0", "d2", "d3"])


if __name__ == "__main__":
    unittest.main()
