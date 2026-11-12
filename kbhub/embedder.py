"""Text embeddings: local Ollama (default) or the bundled offline wordllama model."""
from __future__ import annotations

import contextlib
import shutil
import sys
from pathlib import Path

from .config import GlobalConfig

BATCH = 32
TIMEOUT = 120


class EmbedderUnavailable(Exception):
    """The embedding backend cannot answer right now (e.g. Ollama is not running)."""


class Embedder:
    def __init__(self, cfg: GlobalConfig):
        e = cfg.embedding
        self.cfg = cfg
        self.backend = e.get("backend", "ollama")
        self._model = None
        self._dim: int | None = None
        if self.backend == "ollama":
            self.model_name = e.get("ollama_model", "nomic-embed-text")
            self.url = e.get("ollama_url", "http://localhost:11434").rstrip("/")
            self.id = f"ollama:{self.model_name}"
        elif self.backend == "wordllama":
            self.id = "wordllama:l2_supercat_256"
            self._dim = 256
        else:
            raise ValueError(f"Unknown embedding backend {self.backend!r} (use \"ollama\" or \"wordllama\")")

    @property
    def dim(self) -> int:
        """Vector size. Probed on first use, so building an Embedder never needs Ollama."""
        if self._dim is None:
            self._dim = len(self.embed(["dim probe"])[0])
        return self._dim

    def _wordllama(self):
        if self._model is None:
            import wordllama
            from wordllama import WordLlama
            # The wheel bundles weights + tokenizer; copy the tokenizer to where the loader
            # looks so it never needs network access.
            cache = self.cfg.db_dir / "wordllama-cache"
            (cache / "tokenizers").mkdir(parents=True, exist_ok=True)
            src = Path(wordllama.__file__).parent / "tokenizers" / "l2_supercat_tokenizer_config.json"
            dst = cache / "tokenizers" / src.name
            if src.exists() and not dst.exists():
                shutil.copy(src, dst)
            with contextlib.redirect_stdout(sys.stderr):
                self._model = WordLlama.load(cache_dir=cache, disable_download=src.exists())
        return self._model

    def _ollama(self, texts: list[str], kind: str) -> list[list[float]]:
        import requests
        prefix = ""
        if "nomic" in self.model_name:
            prefix = "search_query: " if kind == "query" else "search_document: "
        vecs: list[list[float]] = []
        try:
            for i in range(0, len(texts), BATCH):
                batch = [prefix + t for t in texts[i:i + BATCH]]
                r = requests.post(f"{self.url}/api/embed", json={"model": self.model_name, "input": batch},
                                  timeout=TIMEOUT)
                r.raise_for_status()
                got = r.json()["embeddings"]
                if len(got) != len(batch):
                    raise ValueError(f"asked for {len(batch)} embeddings, got {len(got)}")
                vecs.extend(got)
        except (OSError, requests.RequestException, ValueError, KeyError, TypeError) as exc:
            raise EmbedderUnavailable(f"Ollama embedding failed at {self.url} ({self.model_name}): {exc}") from exc
        return vecs

    def embed(self, texts: list[str], kind: str = "document") -> list[list[float]]:
        """L2-normalised vectors. `kind` is "document" or "query"."""
        import numpy as np
        if not texts:
            return []
        if self.backend == "wordllama":
            vecs = np.asarray(self._wordllama().embed(texts), dtype="float32")
        else:
            vecs = np.asarray(self._ollama(texts, kind), dtype="float32")
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1
        return (vecs / norms).tolist()
