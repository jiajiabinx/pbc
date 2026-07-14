"""Local embeddings for PBC item candidate matching — $0, deterministic, offline.

Primary: sentence-transformers MiniLM. Fallback (if torch isn't installed or the
model can't be loaded offline): a deterministic hashed character-ngram TF-IDF
cosine, so a cold run never breaks on the matching path.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter

_WORD = re.compile(r"[a-z0-9]+")
_DIM = 512


def _fallback_vector(text: str) -> dict[int, float]:
    words = _WORD.findall(text.lower())
    grams: Counter[int] = Counter()
    for w in words:
        grams[int(hashlib.md5(w.encode()).hexdigest()[:8], 16) % _DIM] += 1.0
        for i in range(len(w) - 2):
            g = w[i:i + 3]
            grams[int(hashlib.md5(g.encode()).hexdigest()[:8], 16) % _DIM] += 0.5
    norm = math.sqrt(sum(v * v for v in grams.values())) or 1.0
    return {k: v / norm for k, v in grams.items()}


def _fallback_cosine(a: dict[int, float], b: dict[int, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


class ItemMatcher:
    """Embeds the PBC item descriptions once; match(query) returns ranked candidates."""

    def __init__(self, items: list[dict]):
        self.items = items
        self.texts = [
            f"{it['item_id']} {it['category']}: {it['description']} {it['acceptance']}"
            for it in items
        ]
        self._st_model = None
        self._vectors = None
        try:
            from sentence_transformers import SentenceTransformer  # noqa: PLC0415
            self._st_model = SentenceTransformer("all-MiniLM-L6-v2")
            self._vectors = self._st_model.encode(self.texts, normalize_embeddings=True)
            self.backend = "sentence-transformers/all-MiniLM-L6-v2"
        except Exception:
            self._fb_vectors = [_fallback_vector(t) for t in self.texts]
            self.backend = "hashed-ngram-tfidf (fallback)"

    def match(self, query: str, top_k: int = 5) -> list[dict]:
        if self._st_model is not None:
            qv = self._st_model.encode([query], normalize_embeddings=True)[0]
            scores = [float(qv @ v) for v in self._vectors]
        else:
            qv = _fallback_vector(query)
            scores = [_fallback_cosine(qv, v) for v in self._fb_vectors]
        ranked = sorted(zip(self.items, scores), key=lambda p: p[1], reverse=True)[:top_k]
        return [
            {"item_id": it["item_id"], "score": round(s, 3),
             "description": it["description"][:160]}
            for it, s in ranked
        ]
