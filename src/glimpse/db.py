"""SQLite database: schema, migrations, connection factory.

All background workers share a single connection factory that returns connections with
sqlite-vec loaded and loadable extensions enabled. The schema follows spec §3 with
the addition of a vec0 virtual table and an FTS5 table for keyword/BM25 search.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from pathlib import Path
from typing import Iterator

import sqlite_vec

from glimpse.config import Paths

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
"""Increment when changing schema; migrations are additive only."""


SCHEMA_SQL = """
-- One row per indexed file
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    drive_or_location_id INTEGER NOT NULL,
    file_type TEXT NOT NULL,         -- text | code | pdf | office | image | video
    content_hash TEXT NOT NULL,      -- SHA-256 hex
    mtime REAL NOT NULL,             -- modification timestamp (float)
    size_bytes INTEGER NOT NULL,
    gist TEXT,                       -- ~300 char summary
    indexed_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'  -- pending | indexed | skipped | error
);

-- One row per searchable chunk (text passage, image region, video keyframe/transcript segment)
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    chunk_type TEXT NOT NULL,        -- text | image | video_frame | video_transcript
    snippet TEXT NOT NULL,           -- ~150 chars, truncated
    position_meta TEXT,              -- JSON: page #, timestamp, region, etc.
    embedding BLOB                   -- raw float32 bytes (fallback if vec0 unavailable)
);

-- Vector index via sqlite-vec (vec0). 384 dims for bge-small-en-v1.5.
-- This table is keyed to chunks.id via rowid.
CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
    embedding float[384]
);

-- FTS5 full-text index for keyword/BM25 search over snippets.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    snippet,
    file_id UNINDEXED,
    content='chunks',
    content_rowid='id'
);

-- Triggers to keep FTS in sync with chunks table.
CREATE TRIGGER IF NOT EXISTS chunks_fts_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, snippet, file_id)
    VALUES (new.id, new.snippet, new.file_id);
END;

CREATE TRIGGER IF NOT EXISTS chunks_fts_ad AFTER DELETE ON chunks BEGIN
    DELETE FROM chunks_fts WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS chunks_fts_au AFTER UPDATE ON chunks BEGIN
    UPDATE chunks_fts SET snippet = new.snippet, file_id = new.file_id
    WHERE rowid = new.id;
END;

-- Trigger to keep vec_chunks in sync with chunks table.
CREATE TRIGGER IF NOT EXISTS vec_chunks_ad AFTER DELETE ON chunks BEGIN
    DELETE FROM vec_chunks WHERE rowid = old.id;
END;

-- User-controlled scope
CREATE TABLE IF NOT EXISTS indexed_locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    added_at REAL NOT NULL
);

-- Per-category toggle, applies to both indexing AND the live watcher
CREATE TABLE IF NOT EXISTS file_type_settings (
    category TEXT PRIMARY KEY,       -- text | code | pdf | office | image | video
    enabled INTEGER NOT NULL DEFAULT 1
);

-- Model provider configs (OpenAI-compatible, Anthropic-compatible, Ollama, or "local only")
-- v0.1: table exists but UI is stubbed; fully used from v0.3.
CREATE TABLE IF NOT EXISTS model_providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,              -- openai | anthropic | ollama | local
    base_url TEXT,
    api_key TEXT,
    model_name TEXT,
    enabled INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 0
);

-- Key/value settings (profile, governor thresholds, max_effort_state, etc.)
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Migration tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
"""


@contextlib.contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a connection with loadable extensions enabled and sqlite-vec loaded.

    Yields a connection configured with:
    - row_factory = sqlite3.Row (named access)
    - loadable extensions enabled
    - sqlite-vec extension loaded (vec0 + functions)
    - foreign keys ON
    - WAL mode for better concurrency
    """
    con = sqlite3.connect(db_path, timeout=30.0)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = NORMAL")
        con.execute("PRAGMA busy_timeout = 30000")
        # Must enable loadable extensions BEFORE loading sqlite-vec
        con.enable_load_extension(True)
        sqlite_vec.load(con)
        yield con
    finally:
        # Checkpoint WAL so subsequent connections see our changes
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        con.close()


def init_db(db_path: Path, *, reset: bool = False) -> None:
    """Initialize the database schema and run migrations.

    Args:
        db_path: Path to the SQLite file.
        reset: If True, drop all tables and recreate (dev/test only).
    """
    if reset and db_path.exists():
        db_path.unlink()

    with connect(db_path) as con:
        # Execute schema as a single transaction
        con.executescript(SCHEMA_SQL)

        # Ensure schema_version row exists and is current
        cur = con.execute("SELECT version FROM schema_version")
        row = cur.fetchone()
        if row is None:
            con.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
        elif row[0] < SCHEMA_VERSION:
            # TODO: implement migrations when schema evolves
            log.warning("Schema version %d < %d; no migrations implemented yet", row[0], SCHEMA_VERSION)
            con.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))

        # Seed default file_type_settings if empty
        cur = con.execute("SELECT COUNT(*) FROM file_type_settings")
        if cur.fetchone()[0] == 0:
            from glimpse.config import DEFAULT_FILE_TYPE_ENABLED
            for cat, enabled in DEFAULT_FILE_TYPE_ENABLED.items():
                con.execute(
                    "INSERT INTO file_type_settings(category, enabled) VALUES (?, ?)",
                    (cat, 1 if enabled else 0),
                )

        # Seed default settings if empty
        cur = con.execute("SELECT COUNT(*) FROM settings")
        if cur.fetchone()[0] == 0:
            from glimpse.config import DEFAULT_SETTINGS
            for k, v in DEFAULT_SETTINGS.items():
                con.execute("INSERT INTO settings(key, value) VALUES (?, ?)", (k, v))

        con.commit()


def get_schema_version(db_path: Path) -> int:
    with connect(db_path) as con:
        cur = con.execute("SELECT version FROM schema_version")
        row = cur.fetchone()
        return row[0] if row else 0