"""Text embedders.

``HashingEmbedder`` is the zero-dependency default: feature hashing of words,
word bigrams and character trigrams into a fixed-size vector. It needs no model
download and no network, so memory works out of the box on an air-gapped
machine. For higher-quality semantic recall point ``AIOS_EMBEDDER`` at any
OpenAI-compatible embeddings endpoint (a local Ollama model, a hosted API).
"""
from __future__ import annotations

import hashlib
import math
import re

import numpy as np

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset("a an the and or of to in on for with is are was were be been it this that as at by from".split())


class HashingEmbedder:
    def __init__(self, dim: int = 768):
        self.dim = dim
        self.id = f"hashing-{dim}"

    def _bucket(self, feature: str) -> tuple[int, float]:
        h = int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "little")
        return h % self.dim, 1.0 if (h >> 63) & 1 else -1.0

    def _one(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        words = [w for w in _WORD.findall(text.lower()) if w not in _STOP]
        counts: dict[str, float] = {}
        for w in words:
            counts[f"w:{w}"] = counts.get(f"w:{w}", 0) + 1.0
            if len(w) > 4:  # crude stemming signal: shared prefixes pull inflections together
                counts[f"p:{w[:5]}"] = counts.get(f"p:{w[:5]}", 0) + 0.5
            for i in range(len(w) - 2):
                counts[f"c:{w[i:i+3]}"] = counts.get(f"c:{w[i:i+3]}", 0) + 0.2
        for a, b in zip(words, words[1:]):
            counts[f"b:{a}_{b}"] = counts.get(f"b:{a}_{b}", 0) + 0.7
        for feat, c in counts.items():
            idx, sign = self._bucket(feat)
            v[idx] += sign * (1.0 + math.log(c)) if c >= 1 else sign * c
        n = float(np.linalg.norm(v))
        return v / n if n else v

    async def embed(self, texts: list[str]) -> np.ndarray:
        return np.stack([self._one(t) for t in texts]) if texts else np.zeros((0, self.dim), dtype=np.float32)


class RemoteEmbedder:
    """Embeddings from an OpenAI-compatible ``/embeddings`` endpoint."""

    def __init__(self, base_url: str, model: str, api_key: str | None = None):
        from ..llm.openai_compat import OpenAICompatProvider
        self._client = OpenAICompatProvider(base_url=base_url, api_key=api_key)
        self.model = model
        self.id = f"remote-{model}"

    async def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 1), dtype=np.float32)
        arr = np.asarray(await self._client.embed(self.model, texts), dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        return arr / np.where(norms == 0, 1, norms)
