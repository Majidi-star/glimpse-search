"""Data access layer: typed operations over the database.

All functions take a connection (from db.connect) and return plain Python types.
No Pydantic models here — those live in models.py for API boundaries.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal, Sequence

from glimpse.db import connect

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Type-ish helpers (simple classes, not Pydantic — keep DB layer light)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class FileRecord:
    id: int
    path: str
    drive_or_location_id: int
    file_type: str
    content_hash: str
    mtime: float
    size_bytes: int
    gist: str | None
    indexed_at: float
    status: str  # pending | indexed | skipped | error


@dataclass(slots=True)
class ChunkRecord:
    id: int
    file_id: int
    chunk_type: str
    snippet: str
    position_meta: str | None
    embedding: bytes | None  # raw float32 bytes


@dataclass(slots=True)
class LocationRecord:
    id: int
    path: str
    enabled: bool
    added_at: float


@dataclass(slots=True)
class SearchHit:
    file_id: int
    path: str
    file_type: str
    snippet: str
    mtime: float
    score: float
    gist: str | None
    chunk_type: str
    position_meta: str | None


# ---------------------------------------------------------------------------
# Hashing & utils
# ---------------------------------------------------------------------------

def compute_content_hash(path: Path) -> str:
    """SHA-256 of file contents, streamed in chunks to avoid loading huge files."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Settings (key/value)
# ---------------------------------------------------------------------------

def get_setting(con: sqlite3.Connection, key: str, default: str = "") -> str:
    row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_all_settings(con: sqlite3.Connection) -> dict[str, str]:
    return {row["key"]: row["value"] for row in con.execute("SELECT key, value FROM settings")}


# ---------------------------------------------------------------------------
# File-type settings
# ---------------------------------------------------------------------------

def get_file_type_settings(con: sqlite3.Connection) -> dict[str, bool]:
    return {row["category"]: bool(row["enabled"]) for row in con.execute("SELECT category, enabled FROM file_type_settings")}


def set_file_type_enabled(con: sqlite3.Connection, category: str, enabled: bool) -> None:
    con.execute(
        "INSERT INTO file_type_settings(category, enabled) VALUES (?, ?) "
        "ON CONFLICT(category) DO UPDATE SET enabled = excluded.enabled",
        (category, 1 if enabled else 0),
    )


# ---------------------------------------------------------------------------
# Indexed locations
# ---------------------------------------------------------------------------

def add_location(con: sqlite3.Connection, path: str, enabled: bool = True) -> int:
    now = time.time()
    cur = con.execute(
        "INSERT INTO indexed_locations(path, enabled, added_at) VALUES (?, ?, ?)",
        (path, 1 if enabled else 0, now),
    )
    return cur.lastrowid


def remove_location(con: sqlite3.Connection, location_id: int) -> None:
    """Remove a location and ALL its file data (cascades to chunks via FK)."""
    # FK on chunks.file_id -> files.id with ON DELETE CASCADE handles chunks.
    # vec_chunks and chunks_fts have triggers/are virtual; we must delete chunks explicitly.
    # But our chunks table has no FK to files that deletes cascading (we use manual deletes below).
    # For safety, do it in order: chunks (and vec/fts via triggers) -> files -> location.
    con.execute("DELETE FROM chunks WHERE file_id IN (SELECT id FROM files WHERE drive_or_location_id = ?)", (location_id,))
    con.execute("DELETE FROM files WHERE drive_or_location_id = ?", (location_id,))
    con.execute("DELETE FROM indexed_locations WHERE id = ?", (location_id,))


def get_locations(con: sqlite3.Connection) -> list[LocationRecord]:
    return [
        LocationRecord(id=row["id"], path=row["path"], enabled=bool(row["enabled"]), added_at=row["added_at"])
        for row in con.execute("SELECT id, path, enabled, added_at FROM indexed_locations ORDER BY added_at")
    ]


def set_location_enabled(con: sqlite3.Connection, location_id: int, enabled: bool) -> None:
    con.execute(
        "UPDATE indexed_locations SET enabled = ? WHERE id = ?",
        (1 if enabled else 0, location_id),
    )


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def upsert_file(
    con: sqlite3.Connection,
    *,
    path: str,
    drive_or_location_id: int,
    file_type: str,
    content_hash: str,
    mtime: float,
    size_bytes: int,
    gist: str | None,
    status: Literal["pending", "indexed", "skipped", "error"] = "pending",
) -> int:
    """Insert or update a file record. Returns the file id."""
    now = time.time()
    cur = con.execute(
        """
        INSERT INTO files(path, drive_or_location_id, file_type, content_hash, mtime, size_bytes, gist, indexed_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            drive_or_location_id = excluded.drive_or_location_id,
            file_type = excluded.file_type,
            content_hash = excluded.content_hash,
            mtime = excluded.mtime,
            size_bytes = excluded.size_bytes,
            gist = excluded.gist,
            indexed_at = excluded.indexed_at,
            status = excluded.status
        """,
        (path, drive_or_location_id, file_type, content_hash, mtime, size_bytes, gist, now, status),
    )
    return cur.lastrowid


def get_file_by_path(con: sqlite3.Connection, path: str) -> FileRecord | None:
    row = con.execute(
        "SELECT id, path, drive_or_location_id, file_type, content_hash, mtime, size_bytes, gist, indexed_at, status "
        "FROM files WHERE path = ?",
        (path,),
    ).fetchone()
    return FileRecord(**row) if row else None


def get_file_by_id(con: sqlite3.Connection, file_id: int) -> FileRecord | None:
    row = con.execute(
        "SELECT id, path, drive_or_location_id, file_type, content_hash, mtime, size_bytes, gist, indexed_at, status "
        "FROM files WHERE id = ?",
        (file_id,),
    ).fetchone()
    return FileRecord(**row) if row else None


def get_files_by_location(con: sqlite3.Connection, location_id: int) -> Iterator[FileRecord]:
    for row in con.execute(
        "SELECT id, path, drive_or_location_id, file_type, content_hash, mtime, size_bytes, gist, indexed_at, status "
        "FROM files WHERE drive_or_location_id = ? ORDER BY path",
        (location_id,),
    ):
        yield FileRecord(**row)


def set_file_status(con: sqlite3.Connection, file_id: int, status: Literal["pending", "indexed", "skipped", "error"]) -> None:
    con.execute("UPDATE files SET status = ? WHERE id = ?", (status, file_id))


def set_file_gist(con: sqlite3.Connection, file_id: int, gist: str) -> None:
    con.execute("UPDATE files SET gist = ? WHERE id = ?", (gist, file_id))


def delete_file(con: sqlite3.Connection, file_id: int) -> None:
    """Delete a file and its chunks (and thus vec/fts via triggers)."""
    con.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))
    con.execute("DELETE FROM files WHERE id = ?", (file_id,))


# ---------------------------------------------------------------------------
# Chunks + embeddings
# ---------------------------------------------------------------------------

def insert_chunks(
    con: sqlite3.Connection,
    file_id: int,
    chunks: list[dict[str, Any]],  # each: chunk_type, snippet, position_meta, embedding (bytes or None)
) -> list[int]:
    """Insert chunks and their embeddings in a single transaction.

    Returns the list of inserted chunk IDs (matching the input order).
    """
    ids: list[int] = []
    for ch in chunks:
        cur = con.execute(
            "INSERT INTO chunks(file_id, chunk_type, snippet, position_meta, embedding) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                file_id,
                ch["chunk_type"],
                ch["snippet"],
                ch.get("position_meta"),
                ch.get("embedding"),
            ),
        )
        cid = cur.lastrowid
        ids.append(cid)
        # vec_chunks: rowid must match chunks.id
        emb = ch.get("embedding")
        if emb is not None:
            con.execute("INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)", (cid, emb))
    return ids


def delete_chunks_for_file(con: sqlite3.Connection, file_id: int) -> None:
    """Delete all chunks for a file (triggers clean up vec_chunks + chunks_fts)."""
    con.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))


def get_chunk_count(con: sqlite3.Connection, file_id: int) -> int:
    row = con.execute("SELECT COUNT(*) FROM chunks WHERE file_id = ?", (file_id,)).fetchone()
    return row[0] if row else 0


# ---------------------------------------------------------------------------
# Search (hybrid: vec + FTS5/BM25)
# ---------------------------------------------------------------------------

def hybrid_search(
    con: sqlite3.Connection,
    *,
    query_embedding: bytes,
    query_text: str,
    vector_weight: float = 0.6,
    keyword_weight: float = 0.4,
    top_k: int = 50,
    location_ids: list[int] | None = None,
    enabled_categories: list[str] | None = None,
) -> list[SearchHit]:
    """Hybrid vector + keyword search with re-ranking.

    Returns top_k hits with combined score. If vec0 is unavailable (unlikely after init),
    falls back to keyword-only via FTS5.
    """
    # Build WHERE clause for location/category filters
    where_clauses = ["f.status = 'indexed'"]
    params: list[Any] = []

    if location_ids is not None:
        if location_ids:
            placeholders = ",".join("?" * len(location_ids))
            where_clauses.append(f"f.drive_or_location_id IN ({placeholders})")
            params.extend(location_ids)
        else:
            # Empty list = match nothing
            where_clauses.append("1=0")

    if enabled_categories is not None:
        if enabled_categories:
            placeholders = ",".join("?" * len(enabled_categories))
            where_clauses.append(f"f.file_type IN ({placeholders})")
            params.extend(enabled_categories)
        else:
            where_clauses.append("1=0")

    where_sql = "WHERE " + " AND ".join(where_clauses)

    # --- Vector search via sqlite-vec ---
    vec_hits: list[tuple[int, float]] = []  # (chunk_id, distance)
    try:
        # We join chunks -> files. vec_chunks rowid = chunks.id.
        sql = f"""
            SELECT c.id AS chunk_id, c.file_id, c.snippet, c.chunk_type, c.position_meta,
                   f.path, f.file_type, f.mtime, f.gist,
                   vec_distance_cosine(vec_chunks.embedding, ?) AS distance
            FROM vec_chunks
            JOIN chunks c ON vec_chunks.rowid = c.id
            JOIN files f ON c.file_id = f.id
            {where_sql}
            ORDER BY distance
            LIMIT ?
        """
        # query_embedding + top_k
        vec_params = [query_embedding] + params + [top_k]
        for row in con.execute(sql, vec_params):
            vec_hits.append((row["chunk_id"], row["distance"], row))
    except Exception as e:
        log.warning("vec search failed, falling back to keyword-only: %s", e)

    # --- Keyword search via FTS5 (BM25) ---
    kw_hits: list[tuple[int, float]] = []  # (chunk_id, bm25)
    if query_text.strip():
        try:
            sql = f"""
                SELECT c.id AS chunk_id, c.file_id, c.snippet, c.chunk_type, c.position_meta,
                       f.path, f.file_type, f.mtime, f.gist,
                       bm25(chunks_fts) AS bm25
                FROM chunks_fts
                JOIN chunks c ON chunks_fts.rowid = c.id
                JOIN files f ON c.file_id = f.id
                {where_sql}
                AND chunks_fts MATCH ?
                ORDER BY bm25
                LIMIT ?
            """
            kw_params = [query_text] + params + [top_k]
            for row in con.execute(sql, kw_params):
                kw_hits.append((row["chunk_id"], row["bm25"], row))
        except Exception as e:
            log.warning("FTS5 search failed: %s", e)

    # --- Merge & re-rank ---
    # Normalize scores to 0..1 range per source, then weighted sum.
    # Distance: smaller is better -> score = 1 - distance (cosine distance in [0,2] for normalized vecs).
    # BM25: smaller is better -> invert: score = 1 / (1 + bm25) roughly.

    def norm_vec(d: float) -> float:
        # cosine distance in [0, 2], so similarity = 1 - distance/2 -> [0,1]
        return max(0.0, 1.0 - d / 2.0)

    def norm_bm25(b: float) -> float:
        # bm25 can be negative for very good matches; cap at reasonable range
        return max(0.0, 1.0 / (1.0 + max(0.0, b)))

    # Combine by chunk_id, keeping the best row data
    combined: dict[int, SearchHit] = {}

    for chunk_id, dist, row in vec_hits:
        score_v = norm_vec(dist)
        combined[chunk_id] = SearchHit(
            file_id=row["file_id"],
            path=row["path"],
            file_type=row["file_type"],
            snippet=row["snippet"],
            mtime=row["mtime"],
            gist=row["gist"],
            chunk_type=row["chunk_type"],
            position_meta=row["position_meta"],
            score=score_v * vector_weight,
        )

    for chunk_id, bm25, row in kw_hits:
        score_k = norm_bm25(bm25) * keyword_weight
        if chunk_id in combined:
            combined[chunk_id].score += score_k
        else:
            combined[chunk_id] = SearchHit(
                file_id=row["file_id"],
                path=row["path"],
                file_type=row["file_type"],
                snippet=row["snippet"],
                mtime=row["mtime"],
                gist=row["gist"],
                chunk_type=row["chunk_type"],
                position_meta=row["position_meta"],
                score=score_k,
            )

    # Sort by combined score descending
    hits = sorted(combined.values(), key=lambda h: h.score, reverse=True)
    return hits[:top_k]


# ---------------------------------------------------------------------------
# Stats / monitoring
# ---------------------------------------------------------------------------

def get_index_stats(con: sqlite3.Connection) -> dict[str, int]:
    """Return counts for monitoring / UI."""
    stats = {}
    stats["files_total"] = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    stats["files_indexed"] = con.execute("SELECT COUNT(*) FROM files WHERE status = 'indexed'").fetchone()[0]
    stats["files_pending"] = con.execute("SELECT COUNT(*) FROM files WHERE status = 'pending'").fetchone()[0]
    stats["files_error"] = con.execute("SELECT COUNT(*) FROM files WHERE status = 'error'").fetchone()[0]
    stats["chunks_total"] = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    stats["locations_total"] = con.execute("SELECT COUNT(*) FROM indexed_locations").fetchone()[0]
    stats["locations_enabled"] = con.execute("SELECT COUNT(*) FROM indexed_locations WHERE enabled = 1").fetchone()[0]
    return stats


def get_file_types(con: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = con.execute("SELECT category, enabled FROM file_type_settings ORDER BY category").fetchall()
    return [{"category": r["category"], "enabled": bool(r["enabled"])} for r in rows]