import math

import pytest
import requests

from kbhub.config import GlobalConfig
from kbhub.embedder import Embedder, EmbedderUnavailable


@pytest.fixture
def cfg(kb_home):
    return GlobalConfig.load()


@pytest.fixture
def ollama_cfg(cfg):
    cfg.embedding = {"backend": "ollama", "ollama_model": "nomic-embed-text", "ollama_url": "http://localhost:11434/"}
    return cfg


class FakeResponse:
    def __init__(self, n, status=200):
        self.n, self.status = n, status

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(f"{self.status} error")

    def json(self):
        return {"embeddings": [[3.0, 4.0, 0.0]] * self.n}


def test_wordllama_offline_normalised(cfg):
    emb = Embedder(cfg)
    assert emb.id == "wordllama:l2_supercat_256" and emb.dim == 256
    vecs = emb.embed(["a", "b"])
    assert len(vecs) == 2 and all(len(v) == 256 for v in vecs)
    assert all(math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, abs_tol=1e-4) for v in vecs)
    assert emb.embed([]) == []


def test_ollama_prefix_and_batching(ollama_cfg, monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append((url, json, timeout))
        return FakeResponse(len(json["input"]))

    monkeypatch.setattr(requests, "post", fake_post)
    emb = Embedder(ollama_cfg)
    assert emb.id == "ollama:nomic-embed-text"
    vecs = emb.embed([f"text {i}" for i in range(70)])
    assert len(calls) == 3 and [len(c[1]["input"]) for c in calls] == [32, 32, 6]
    assert calls[0][0] == "http://localhost:11434/api/embed" and calls[0][2] == 120
    assert calls[0][1]["model"] == "nomic-embed-text"
    assert calls[0][1]["input"][0] == "search_document: text 0"
    assert len(vecs) == 70 and vecs[0] == pytest.approx([0.6, 0.8, 0.0])
    calls.clear()
    emb.embed(["q"], kind="query")
    assert calls[0][1]["input"] == ["search_query: q"]


def test_ollama_dim_probed_lazily_once(ollama_cfg, monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append(json["input"])
        return FakeResponse(len(json["input"]))

    monkeypatch.setattr(requests, "post", fake_post)
    emb = Embedder(ollama_cfg)
    assert calls == []            # constructing never needs Ollama
    assert emb.dim == 3 and emb.dim == 3
    assert len(calls) == 1 and "dim probe" in calls[0][0]


def test_ollama_down_raises(ollama_cfg, monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("refused")

    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(EmbedderUnavailable):
        Embedder(ollama_cfg).embed(["a"])
    monkeypatch.setattr(requests, "post", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("x")))
    with pytest.raises(EmbedderUnavailable):
        Embedder(ollama_cfg).embed(["a"])


def test_ollama_http_error_raises_unavailable(ollama_cfg, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse(1, status=404))
    with pytest.raises(EmbedderUnavailable, match="404"):
        Embedder(ollama_cfg).embed(["a"])


def test_unknown_backend_rejected(cfg):
    cfg.embedding = {"backend": "nope"}
    with pytest.raises(ValueError, match="nope"):
        Embedder(cfg)
