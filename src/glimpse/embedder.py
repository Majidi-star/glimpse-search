"""Embedder interface and implementations.

- `Embedder`: ABC with `dim` and `embed_texts(texts) -> np.ndarray[float32, shape=(N, dim)]`
- `HashingEmbedder`: deterministic, no network, pure Python. Uses MD5 + splitmix64 to
  produce 384-dim pseudo-random vectors. Good enough for dev / offline / no-torch.
- `SentenceTransformersEmbedder`: wraps `sentence-transformers` + `BAAI/bge-small-en-v1.5`.
  Only available if the `models` extra is installed (`uv sync --extra models`).
  Lazily loads on first use; surfaces "preparing model..." state via `is_ready()`.

All embedders produce normalized vectors (L2 norm = 1) so cosine similarity = dot product.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from abc import ABC, abstractmethod
from typing import Final

import numpy as np

from glimpse.config import RuntimeFlags

log = logging.getLogger(__name__)

# bge-small-en-v1.5 dimension
EMBED_DIM: Final[int] = 384


class Embedder(ABC):
    """Abstract embedder interface."""

    @property
    @abstractmethod
    def dim(self) -> int:
        """Embedding dimension (must match vec_chunks table)."""
        ...

    @abstractmethod
    def embed_texts(self, texts: list[str]) -> np.ndarray:
        """Embed a batch of texts.

        Returns: float32 array of shape (len(texts), dim), L2-normalized rows.
        """
        ...

    def is_ready(self) -> bool:
        """Whether the embedder is fully loaded and ready to use.

        For hashing embedder: always True.
        For model embedder: True after model is downloaded and loaded.
        """
        return True


# ---------------------------------------------------------------------------
# Hashing embedder (fallback, no dependencies)
# ---------------------------------------------------------------------------

# splitmix64 constants
_SPLITMIX64_C1 = 0x9E3779B97F4A7C15
_SPLITMIX64_C2 = 0xBF58476D1CE4E5B9
_SPLITMIX64_C3 = 0x94D049BB133111EB


def _splitmix64(x: int) -> int:
    x = (x + _SPLITMIX64_C1) & 0xFFFFFFFFFFFFFFFF
    z = x
    z = (z ^ (z >> 30)) * _SPLITMIX64_C2 & 0xFFFFFFFFFFFFFFFF
    z = (z ^ (z >> 27)) * _SPLITMIX64_C3 & 0xFFFFFFFFFFFFFFFF
    return z ^ (z >> 31)


class HashingEmbedder(Embedder):
    """Deterministic hashing-based embedder for offline/dev use.

    Not semantically meaningful, but:
    - Same text always produces same vector
    - Different texts produce different vectors (with high probability)
    - No network, no torch, no model download
    - 384-dim to match bge-small-en-v1.5
    """

    def __init__(self, dim: int = EMBED_DIM):
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, self._dim), dtype=np.float32)

        out = np.empty((len(texts), self._dim), dtype=np.float32)
        for i, text in enumerate(texts):
            # Hash the text to get a seed
            h = hashlib.md5(text.encode("utf-8")).digest()
            seed = int.from_bytes(h[:8], "little")

            # Generate dim pseudo-random values from the seed
            vec = np.empty(self._dim, dtype=np.float32)
            x = seed
            for d in range(self._dim):
                x = _splitmix64(x)
                # Convert to float in [-1, 1]
                vec[d] = (x & 0xFFFFFFFF) / 0xFFFFFFFF * 2.0 - 1.0

            # L2 normalize
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            out[i] = vec

        return out

    def is_ready(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# SentenceTransformers embedder (real semantic embeddings)
# ---------------------------------------------------------------------------


class _SentenceTransformersEmbedder(Embedder):
    """Wraps sentence-transformers BAAI/bge-small-en-v1.5.

    Lazy-loads on first `embed_texts` call. If the `models` extra is not installed,
    or the model fails to load, falls back to HashingEmbedder.
    """

    _model = None
    _load_lock = threading.Lock()
    _load_error: str | None = None
    _ready = False

    def __init__(self):
        self._dim = EMBED_DIM

    @property
    def dim(self) -> int:
        return self._dim

    def is_ready(self) -> bool:
        return self._ready

    def _ensure_loaded(self) -> bool:
        if self._ready:
            return True
        if self._load_error:
            return False

        with self._load_lock:
            if self._ready:
                return True
            if self._load_error:
                return False

            try:
                log.info("Loading embedding model (BAAI/bge-small-en-v1.5)...")
                from sentence_transformers import SentenceTransformer

                self._model = SentenceTransformer("BAAI/bge-small-en-v1.5", device="cpu")
                self._model.eval()
                self._ready = True
                log.info("Embedding model loaded successfully")
                return True
            except Exception as e:
                self._load_error = str(e)
                log.warning("Failed to load sentence-transformers model: %s", e)
                return False

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, self._dim), dtype=np.float32)

        if not self._ensure_loaded():
            # Fall back to hashing embedder
            log.warning("Falling back to hashing embedder")
            return HashingEmbedder(self._dim).embed_texts(texts)

        try:
            # sentence-transformers returns normalized embeddings by default for bge
            embeddings = self._model.encode(
                texts,
                batch_size=32,
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            return embeddings.astype(np.float32)
        except Exception as e:
            log.error("Embedding failed: %s", e)
            # Fall back
            return HashingEmbedder(self._dim).embed_texts(texts)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_EMBEDDER_INSTANCE: Embedder | None = None
_EMBEDDER_LOCK = threading.Lock()


def get_embedder(flags: RuntimeFlags | None = None) -> Embedder:
    """Get the global embedder instance.

    Priority:
    1. If `flags.embed_offline` or `--extra models` not installed -> HashingEmbedder
    2. Else -> SentenceTransformersEmbedder (lazy load)
    """
    global _EMBEDDER_INSTANCE

    if _EMBEDDER_INSTANCE is not None:
        return _EMBEDDER_INSTANCE

    with _EMBEDDER_LOCK:
        if _EMBEDDER_INSTANCE is not None:
            return _EMBEDDER_INSTANCE

        use_real_model = False
        if flags is None or not flags.embed_offline:
            # Check if sentence-transformers is available
            try:
                import sentence_transformers  # noqa: F401

                use_real_model = True
            except ImportError:
                log.info("sentence-transformers not installed; using hashing embedder")

        if use_real_model:
            _EMBEDDER_INSTANCE = _SentenceTransformersEmbedder()
            log.info("Using SentenceTransformers embedder (bge-small-en-v1.5)")
        else:
            _EMBEDDER_INSTANCE = HashingEmbedder()
            log.info("Using HashingEmbedder (offline/deterministic)")

        return _EMBEDDER_INSTANCE


def serialize_embedding(vec: np.ndarray) -> bytes:
    """Serialize a float32 vector to bytes for sqlite-vec (little-endian)."""
    return vec.astype(np.float32).tobytes()


def deserialize_embedding(data: bytes, dim: int = EMBED_DIM) -> np.ndarray:
    """Deserialize bytes to float32 vector."""
    return np.frombuffer(data, dtype=np.float32, count=dim)
