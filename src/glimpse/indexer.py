"""Indexer: orchestrates extraction -> embedding -> storage for a single file.

This is the core indexing pipeline. Called by the worker thread for each job
from the JobQueue. Handles:
- Hash check (mtime + sha256) -> no-op if unchanged
- Extract (via registered extractor)
- Embed chunks (batched)
- Store in DB (files + chunks + vec_chunks + FTS5)
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Callable

from glimpse.config import V01_SUPPORTED_CATEGORIES
from glimpse.db import connect
from glimpse.embedder import get_embedder, serialize_embedding, EMBED_DIM
from glimpse.extractor.base import get_extractor, ExtractionResult
from glimpse.queue import IndexJob, JobPriority
from glimpse.store import (
    upsert_file,
    get_file_by_path,
    get_file_by_id,
    set_file_status,
    set_file_gist,
    delete_chunks_for_file,
    insert_chunks,
    compute_content_hash,
)

log = logging.getLogger(__name__)


class Indexer:
    """Handles the full indexing pipeline for a single file."""

    def __init__(
        self,
        db_path: Path,
        embedder_flags=None,
        batch_size: int = 32,
    ):
        self._db_path = db_path
        self._embedder = get_embedder(embedder_flags)
        self._batch_size = batch_size

    def process(self, job: IndexJob) -> bool:
        """Process a single indexing job.

        Returns True on success, False on failure.
        """
        path = Path(job.path)

        # Quick existence check
        if not path.exists() or not path.is_file():
            log.debug("File not found, marking skipped: %s", path)
            self._mark_skipped(job.file_id, "file_not_found")
            return True  # not a failure, just nothing to do

        # Determine file type category
        category = self._path_to_category(path)
        if not category or category not in V01_SUPPORTED_CATEGORIES:
            log.debug("Unsupported category for %s: %s", path, category)
            self._mark_skipped(job.file_id, "unsupported_type")
            return True

        # Compute hash + mtime
        try:
            stat = path.stat()
            mtime = stat.st_mtime
            size = stat.st_size
            content_hash = compute_content_hash(path)
        except Exception as e:
            log.warning("Failed to stat/hash %s: %s", path, e)
            return False

        # Check if already indexed with same hash+mtime
        with connect(self._db_path) as con:
            existing = get_file_by_path(con, str(path))
            if existing and existing.content_hash == content_hash and abs(existing.mtime - mtime) < 1.0:
                # No-op: unchanged
                log.debug("File unchanged, skipping: %s", path)
                set_file_status(con, existing.id, "indexed")
                con.commit()
                return True

        # Get or create file record
        with connect(self._db_path) as con:
            if existing:
                file_id = existing.id
                # Update metadata
                upsert_file(
                    con,
                    path=str(path),
                    drive_or_location_id=existing.drive_or_location_id,
                    file_type=category,
                    content_hash=content_hash,
                    mtime=mtime,
                    size_bytes=size,
                    gist=existing.gist,  # will update after extraction
                    status="pending",
                )
            else:
                # New file - need a location_id. For watcher-originated jobs,
                # we should have this. For manual rescan, we'll need to find it.
                # For v0.1, use a default location_id=1 (will be fixed when locations API is wired)
                file_id = upsert_file(
                    con,
                    path=str(path),
                    drive_or_location_id=1,  # TODO: proper location lookup
                    file_type=category,
                    content_hash=content_hash,
                    mtime=mtime,
                    size_bytes=size,
                    gist=None,
                    status="pending",
                )
            con.commit()

        # Extract
        extractor = get_extractor(category)
        if not extractor:
            log.warning("No extractor for category %s", category)
            self._mark_error(file_id, "no_extractor")
            return False

        try:
            log.debug("Extracting %s", path)
            result: ExtractionResult = extractor.extract(path)
        except Exception as e:
            log.exception("Extraction failed for %s: %s", path, e)
            self._mark_error(file_id, f"extraction_failed: {e}")
            return False

        if not result.chunks:
            log.debug("No chunks extracted from %s", path)
            self._mark_indexed(file_id, result.gist)
            return True

        # Embed chunks in batches
        chunk_texts = [c.snippet for c in result.chunks]
        embeddings: list[bytes] = []

        for i in range(0, len(chunk_texts), self._batch_size):
            batch = chunk_texts[i:i + self._batch_size]
            try:
                vecs = self._embedder.embed_texts(batch)
                for vec in vecs:
                    embeddings.append(serialize_embedding(vec))
            except Exception as e:
                log.exception("Embedding failed for batch: %s", e)
                # Fall back to hashing embedder for this batch
                from glimpse.embedder import HashingEmbedder
                fallback = HashingEmbedder(EMBED_DIM)
                vecs = fallback.embed_texts(batch)
                for vec in vecs:
                    embeddings.append(serialize_embedding(vec))

        # Prepare chunk records
        chunk_records = []
        for chunk, emb in zip(result.chunks, embeddings):
            chunk_records.append({
                "chunk_type": chunk.chunk_type,
                "snippet": chunk.snippet,
                "position_meta": chunk.position_meta,
                "embedding": emb,
            })

        # Store in DB
        try:
            with connect(self._db_path) as con:
                # Delete old chunks (for re-index)
                delete_chunks_for_file(con, file_id)

                # Insert new chunks + embeddings
                insert_chunks(con, file_id, chunk_records)

                # Update file with gist + status
                set_file_gist(con, file_id, result.gist)
                set_file_status(con, file_id, "indexed")

                con.commit()
        except Exception as e:
            log.exception("DB store failed for %s: %s", path, e)
            self._mark_error(file_id, f"store_failed: {e}")
            return False

        log.info("Indexed %s: %d chunks, gist=%s...", path, len(chunk_records), result.gist[:50])
        return True

    # ---------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------

    def _path_to_category(self, path: Path) -> str | None:
        ext = path.suffix.lower()
        if ext in {".txt", ".md", ".markdown", ".rst", ".text"}:
            return "text"
        if ext in {".py", ".pyw", ".pyi", ".js", ".jsx", ".mjs", ".cjs",
                   ".ts", ".tsx", ".go", ".rs", ".java", ".cpp", ".cc", ".cxx",
                   ".hpp", ".h", ".hxx", ".cs", ".rb", ".php"}:
            return "code"
        if ext == ".pdf":
            return "pdf"
        if ext in {".docx", ".doc", ".odt"}:
            return "office"
        if ext in {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff"}:
            return "image"
        if ext in {".mp4", ".mov", ".avi", ".mkv", ".webm"}:
            return "video"
        return None

    def _mark_skipped(self, file_id: int, reason: str) -> None:
        with connect(self._db_path) as con:
            set_file_status(con, file_id, "skipped")
            con.commit()

    def _mark_indexed(self, file_id: int, gist: str) -> None:
        with connect(self._db_path) as con:
            set_file_gist(con, file_id, gist)
            set_file_status(con, file_id, "indexed")
            con.commit()

    def _mark_error(self, file_id: int, reason: str) -> None:
        with connect(self._db_path) as con:
            set_file_status(con, file_id, "error")
            con.commit()


def create_indexer(db_path: Path, embedder_flags=None, batch_size: int = 32) -> Indexer:
    """Factory for creating an indexer."""
    return Indexer(db_path, embedder_flags, batch_size)