"""Extractor for plain text and Markdown files (.txt, .md).

Chunking strategy (spec §4):
- Split by blank lines (paragraphs)
- Cap at ~150 chunks per file
- Beyond cap: hierarchical summarization (merge adjacent chunks)
- File gist = first ~300 chars of the first substantial paragraph
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from glimpse.extractor.base import (
    GIST_MAX_CHARS,
    SNIPPET_MAX_CHARS,
    TEXT_CHUNK_CAP,
    Chunk,
    ExtractionResult,
    merge_chunks_hierarchical,
    register,
    truncate,
)

log = logging.getLogger(__name__)

TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".rst", ".text"}


class TextExtractor:
    category = "text"

    def can_handle(self, path: Path) -> bool:
        return path.suffix.lower() in TEXT_EXTENSIONS

    def extract(self, path: Path) -> ExtractionResult:
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            log.warning("Failed to read %s: %s", path, e)
            return ExtractionResult(gist="", chunks=[])

        # Split into paragraphs (blank line separated)
        # Handle both \n\n and \r\n\r\n
        paragraphs = re.split(r"\n\s*\n", content)

        # Filter out empty paragraphs and truncate each to snippet size
        chunks: list[Chunk] = []
        for idx, para in enumerate(paragraphs):
            para = para.strip()
            if not para:
                continue
            snippet = truncate(para, SNIPPET_MAX_CHARS)
            # Position meta: paragraph index
            import json

            position_meta = json.dumps({"paragraph": idx})
            chunks.append(Chunk(chunk_type="text", snippet=snippet, position_meta=position_meta))

            if len(chunks) >= TEXT_CHUNK_CAP * 2:  # early stop before hierarchical merge
                break

        # Hierarchical merge if over cap
        if len(chunks) > TEXT_CHUNK_CAP:
            chunks = merge_chunks_hierarchical(chunks, TEXT_CHUNK_CAP)

        # Gist: first substantial paragraph, truncated
        gist = ""
        for para in paragraphs:
            para = para.strip()
            if len(para) > 50:  # skip tiny headers/etc.
                gist = truncate(para, GIST_MAX_CHARS)
                break
        if not gist and paragraphs:
            gist = truncate(paragraphs[0].strip(), GIST_MAX_CHARS)

        return ExtractionResult(gist=gist, chunks=chunks)


register(TextExtractor())
