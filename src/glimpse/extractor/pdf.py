"""Extractor for PDF files (.pdf).

Uses pypdf (pure Python) to extract text per page.
Chunking: per page/paragraph, cap at ~200 chunks, hierarchical merge beyond cap.
File gist = synthesis of first few pages' leading text.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pypdf

from glimpse.extractor.base import (
    Chunk,
    ExtractionResult,
    Extractor,
    GIST_MAX_CHARS,
    SNIPPET_MAX_CHARS,
    PDF_CHUNK_CAP,
    merge_chunks_hierarchical,
    register,
    truncate,
)

log = logging.getLogger(__name__)


class PdfExtractor:
    category = "pdf"

    def can_handle(self, path: Path) -> bool:
        return path.suffix.lower() == ".pdf"

    def extract(self, path: Path) -> ExtractionResult:
        chunks: list[Chunk] = []
        gist_parts: list[str] = []

        try:
            reader = pypdf.PdfReader(str(path))
        except Exception as e:
            log.warning("Failed to open PDF %s: %s", path, e)
            return ExtractionResult(gist="", chunks=[])

        for page_idx, page in enumerate(reader.pages):
            try:
                text = page.extract_text() or ""
            except Exception as e:
                log.debug("Failed to extract text from page %d of %s: %s", page_idx, path, e)
                continue

            text = text.strip()
            if not text:
                continue

            # Collect for gist (first ~3 pages)
            if page_idx < 3 and len(text) > 50:
                gist_parts.append(text[:500])

            # Split page into paragraphs
            paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

            for para_idx, para in enumerate(paragraphs):
                snippet = truncate(para, SNIPPET_MAX_CHARS)
                import json
                position_meta = json.dumps({"page": page_idx + 1, "paragraph": para_idx})
                chunks.append(Chunk(chunk_type="text", snippet=snippet, position_meta=position_meta))

                if len(chunks) >= PDF_CHUNK_CAP * 2:  # early stop before merge
                    break

            if len(chunks) >= PDF_CHUNK_CAP * 2:
                break

        # Hierarchical merge if over cap
        if len(chunks) > PDF_CHUNK_CAP:
            chunks = merge_chunks_hierarchical(chunks, PDF_CHUNK_CAP)

        # Gist: combine first few pages' text
        gist = ""
        if gist_parts:
            gist = truncate(" ".join(gist_parts), GIST_MAX_CHARS)
        elif chunks:
            # Fallback: first chunk
            gist = truncate(chunks[0].snippet, GIST_MAX_CHARS)

        return ExtractionResult(gist=gist, chunks=chunks)


register(PdfExtractor())