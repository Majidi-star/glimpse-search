"""Extractor base protocol and registry.

All extractors implement the ``Extractor`` protocol. The registry maps a file-type
category (from config.FILE_TYPE_CATEGORIES) to an extractor instance. v0.1 ships
text/code/pdf; office/image/video extractors are registered as stubs that return
empty results (they'll be implemented in v0.2+).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from glimpse.config import FILE_TYPE_CATEGORIES

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Chunk:
    """A searchable chunk extracted from a file.

    Fields match the ``chunks`` table (store.py).
    """

    chunk_type: str  # text | image | video_frame | video_transcript
    snippet: str  # ~150 chars, truncated for display
    position_meta: str | None = None  # JSON string: page #, timestamp, region, etc.
    embedding: bytes | None = None  # filled in later by the indexer


@dataclass(slots=True)
class ExtractionResult:
    """Result of extracting a single file."""

    gist: str  # ~300 char file summary
    chunks: list[Chunk]  # searchable chunks


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Extractor(Protocol):
    """Protocol for file-type-specific extractors.

    All extractors must implement:
    - ``category``: the file-type category this extractor handles
    - ``can_handle(path)``: quick check (extension / magic bytes) before expensive work
    - ``extract(path)``: the actual extraction, returning gist + chunks
    """

    category: str

    def can_handle(self, path: Path) -> bool: ...

    def extract(self, path: Path) -> ExtractionResult: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SNIPPET_MAX_CHARS = 150
GIST_MAX_CHARS = 300
TEXT_CHUNK_CAP = 150
PDF_CHUNK_CAP = 200


def truncate(text: str, max_chars: int) -> str:
    """Truncate to max_chars, preserving word boundaries where possible."""
    if len(text) <= max_chars:
        return text
    # Try to cut at a word boundary
    cut = text[:max_chars].rstrip()
    last_space = cut.rfind(" ")
    if last_space > max_chars * 0.7:  # not too aggressive
        cut = cut[:last_space]
    return cut + "…"


def merge_chunks_hierarchical(chunks: list[Chunk], cap: int) -> list[Chunk]:
    """Hierarchical summarization: merge adjacent chunks until under cap.

    v0.1 simplification (spec §4): no model-based summarization. We merge by
    concatenating adjacent chunks' snippets and re-truncating. This is a
    deterministic, no-op for short docs, and a clear placeholder for v0.3+.
    """
    if len(chunks) <= cap:
        return chunks

    # Simple strategy: merge pairs until under cap
    while len(chunks) > cap:
        new_chunks: list[Chunk] = []
        i = 0
        while i < len(chunks):
            if i + 1 < len(chunks):
                # Merge pair
                merged_snippet = truncate(
                    chunks[i].snippet + " " + chunks[i + 1].snippet, SNIPPET_MAX_CHARS
                )
                merged_meta = None
                if chunks[i].position_meta or chunks[i + 1].position_meta:
                    import json

                    m1 = json.loads(chunks[i].position_meta) if chunks[i].position_meta else {}
                    m2 = (
                        json.loads(chunks[i + 1].position_meta)
                        if chunks[i + 1].position_meta
                        else {}
                    )
                    # Merge page ranges etc.
                    if "page" in m1 or "page" in m2:
                        pages = sorted(
                            set(
                                [m1.get("page")]
                                if m1.get("page")
                                else [] + [m2.get("page")]
                                if m2.get("page")
                                else []
                            )
                        )
                        if len(pages) == 1:
                            merged_meta = json.dumps({"page": pages[0]})
                        elif len(pages) > 1:
                            merged_meta = json.dumps({"pages": pages})
                new_chunks.append(
                    Chunk(
                        chunk_type=chunks[i].chunk_type,
                        snippet=merged_snippet,
                        position_meta=merged_meta,
                    )
                )
                i += 2
            else:
                new_chunks.append(chunks[i])
                i += 1
        chunks = new_chunks
    return chunks


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Extractor] = {}


def register(extractor: Extractor) -> None:
    """Register an extractor for its category."""
    if extractor.category in _REGISTRY:
        log.warning("Overwriting extractor for category %r", extractor.category)
    _REGISTRY[extractor.category] = extractor


def get_extractor(category: str) -> Extractor | None:
    """Get the extractor for a category, or None if not registered."""
    return _REGISTRY.get(category)


def get_all_extractors() -> list[Extractor]:
    """Return all registered extractors in category order."""
    return [_REGISTRY[cat] for cat in FILE_TYPE_CATEGORIES if cat in _REGISTRY]


# ---------------------------------------------------------------------------
# Stub extractors for future milestones (office, image, video)
# ---------------------------------------------------------------------------


class _StubExtractor:
    """Extractor that logs a warning and returns empty results (v0.1)."""

    def __init__(self, category: str, milestone: str):
        self.category = category
        self._milestone = milestone

    def can_handle(self, path: Path) -> bool:
        return False  # never called in v0.1 since file_type_settings disables these

    def extract(self, path: Path) -> ExtractionResult:
        log.warning(
            "Extractor for %s not implemented until %s; returning empty",
            self.category,
            self._milestone,
        )
        return ExtractionResult(gist="", chunks=[])


# Register stubs so the registry has all categories (UI toggles stay stable)
for cat in FILE_TYPE_CATEGORIES:
    if cat not in {"text", "code", "pdf"}:  # real ones below
        register(_StubExtractor(cat, "v0.2+" if cat in {"office", "image"} else "v0.4"))
