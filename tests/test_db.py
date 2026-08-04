"""Tests for database schema and sqlite-vec."""

import tempfile
from pathlib import Path

import pytest

from glimpse.db import SCHEMA_VERSION, connect, init_db
from glimpse.store import add_location, upsert_file


class TestDatabase:
    def setup_method(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.sqlite"
        init_db(self.db_path)

    def teardown_method(self):
        self.tmpdir.cleanup()

    def test_schema_created(self):
        with connect(self.db_path) as con:
            # Check all tables exist
            tables = [
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
            expected = {
                "files",
                "chunks",
                "vec_chunks",
                "chunks_fts",
                "indexed_locations",
                "file_type_settings",
                "model_providers",
                "settings",
                "schema_version",
            }
            for t in expected:
                assert t in tables, f"Missing table: {t}"

    def test_vec_chunks_works(self):
        with connect(self.db_path) as con:
            import numpy as np
            import sqlite_vec

            vec = np.random.rand(384).astype(np.float32)
            con.execute(
                "INSERT INTO vec_chunks(rowid, embedding) VALUES (1, ?)",
                (sqlite_vec.serialize_float32(vec),),
            )
            row = con.execute(
                "SELECT vec_distance_cosine(embedding, ?) FROM vec_chunks WHERE rowid=1",
                (sqlite_vec.serialize_float32(vec),),
            ).fetchone()
            assert row[0] < 0.001  # distance to self ~ 0

    def test_fts5_works(self):
        with connect(self.db_path) as con:
            from glimpse.store import add_location, upsert_file

            loc_id = add_location(con, "/test")
            file_id = upsert_file(
                con,
                path="/test/file.txt",
                drive_or_location_id=loc_id,
                file_type="text",
                content_hash="abc",
                mtime=1000.0,
                size_bytes=100,
                gist="test",
                status="indexed",
            )
            con.execute(
                "INSERT INTO chunks(id, file_id, chunk_type, snippet) VALUES (1, ?, 'text', 'hello world')",
                (file_id,),
            )
            con.execute(
                "INSERT INTO chunks(id, file_id, chunk_type, snippet) VALUES (2, ?, 'text', 'foo bar')",
                (file_id,),
            )
            rows = con.execute(
                "SELECT snippet FROM chunks_fts WHERE chunks_fts MATCH 'hello'"
            ).fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "hello world"

    def test_schema_version(self):
        with connect(self.db_path) as con:
            row = con.execute("SELECT version FROM schema_version").fetchone()
            assert row[0] == SCHEMA_VERSION

    def test_file_type_settings_seeded(self):
        with connect(self.db_path) as con:
            rows = con.execute("SELECT category, enabled FROM file_type_settings").fetchall()
            assert len(rows) == 6  # text, code, pdf, office, image, video
            enabled = {r[0]: r[1] for r in rows}
            assert enabled["text"] == 1
            assert enabled["code"] == 1
            assert enabled["pdf"] == 1
            assert enabled["office"] == 0
            assert enabled["image"] == 0
            assert enabled["video"] == 0

    def test_settings_seeded(self):
        with connect(self.db_path) as con:
            rows = con.execute("SELECT key, value FROM settings").fetchall()
            settings = {r[0]: r[1] for r in rows}
            assert settings["profile"] == "balanced"
            assert settings["max_effort"] == "0"
            assert settings["paused"] == "0"


class TestStore:
    def setup_method(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.sqlite"
        init_db(self.db_path)

    def teardown_method(self):
        self.tmpdir.cleanup()

    def test_location_crud(self):
        with connect(self.db_path) as con:
            loc_id = add_location(con, "/home/user/docs")
            con.commit()
            loc_id2 = add_location(con, "/home/user/code")
            con.commit()

            from glimpse.store import get_locations, remove_location, set_location_enabled

            locs = get_locations(con)
            assert len(locs) == 2

            set_location_enabled(con, loc_id, False)
            con.commit()
            locs = get_locations(con)
            assert locs[0].enabled is False

            remove_location(con, loc_id)
            con.commit()
            locs = get_locations(con)
            assert len(locs) == 1

    def test_file_upsert_noop_on_unchanged(self):
        with connect(self.db_path) as con:
            # Add location first
            loc_id = add_location(con, "/home/user/docs")
            con.commit()

            # Insert file
            file_id1 = upsert_file(
                con,
                path="/home/user/docs/readme.md",
                drive_or_location_id=loc_id,
                file_type="text",
                content_hash="abc123",
                mtime=1000.0,
                size_bytes=100,
                gist="test",
                status="indexed",
            )
            con.commit()

            # Upsert with same hash+mtime -> should return same ID
            file_id2 = upsert_file(
                con,
                path="/home/user/docs/readme.md",
                drive_or_location_id=loc_id,
                file_type="text",
                content_hash="abc123",
                mtime=1000.0,
                size_bytes=100,
                gist="test",
                status="indexed",
            )
            con.commit()

            assert file_id1 == file_id2

            # Different hash -> new file (but same path, so updates)
            file_id3 = upsert_file(
                con,
                path="/home/user/docs/readme.md",
                drive_or_location_id=loc_id,
                file_type="text",
                content_hash="def456",
                mtime=2000.0,
                size_bytes=200,
                gist="updated",
                status="indexed",
            )
            con.commit()

            assert file_id3 == file_id1  # same path, upsert updates
            from glimpse.store import get_file_by_path

            f = get_file_by_path(con, "/home/user/docs/readme.md")
            assert f.content_hash == "def456"

    def test_remove_location_cascades(self):
        with connect(self.db_path) as con:
            loc_id = add_location(con, "/home/user/docs")
            file_id = upsert_file(
                con,
                path="/home/user/docs/readme.md",
                drive_or_location_id=loc_id,
                file_type="text",
                content_hash="abc123",
                mtime=1000.0,
                size_bytes=100,
                gist="test",
                status="indexed",
            )
            con.commit()

            from glimpse.store import get_index_stats, remove_location

            stats = get_index_stats(con)
            assert stats["files_total"] == 1

            remove_location(con, loc_id)
            con.commit()

            stats = get_index_stats(con)
            assert stats["files_total"] == 0
            assert stats["locations_total"] == 0

    def test_file_type_settings(self):
        with connect(self.db_path) as con:
            from glimpse.store import get_file_type_settings, set_file_type_enabled

            settings = get_file_type_settings(con)
            assert settings["text"] is True

            set_file_type_enabled(con, "text", False)
            con.commit()

            settings = get_file_type_settings(con)
            assert settings["text"] is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
