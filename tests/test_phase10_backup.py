"""Tests for Phase 10 Part I: Database Backup."""

from __future__ import annotations

import gzip
import sqlite3
from pathlib import Path

import pytest


class TestBackupDatabase:

    def _make_db(self, path: Path) -> Path:
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE test (id INTEGER, val TEXT)")
        conn.execute("INSERT INTO test VALUES (1, 'hello')")
        conn.commit()
        conn.close()
        return path

    def test_backup_creates_file(self, tmp_path):
        from src.backup_database import backup_database
        db_path = tmp_path / "test.db"
        self._make_db(db_path)
        result = backup_database(db_path, tmp_path / "backups")
        assert result.exists()
        assert result.stat().st_size > 0

    def test_backup_content_valid(self, tmp_path):
        from src.backup_database import backup_database
        db_path = tmp_path / "test.db"
        self._make_db(db_path)
        backup_path = backup_database(db_path, tmp_path / "backups")
        conn = sqlite3.connect(str(backup_path))
        row = conn.execute("SELECT val FROM test WHERE id=1").fetchone()
        conn.close()
        assert row[0] == "hello"

    def test_backup_compression(self, tmp_path):
        from src.backup_database import backup_database
        db_path = tmp_path / "test.db"
        self._make_db(db_path)
        result = backup_database(db_path, tmp_path / "backups", compress=True)
        assert str(result).endswith(".db.gz")

    def test_backup_prunes_old(self, tmp_path):
        from src.backup_database import backup_database
        db_path = tmp_path / "test.db"
        self._make_db(db_path)
        backup_dir = tmp_path / "backups"
        for _ in range(5):
            backup_database(db_path, backup_dir, retention_count=3)
        backups = list(backup_dir.glob("mlb_backup_*"))
        assert len(backups) <= 3

    def test_restore_without_confirm_raises(self):
        from src.backup_database import restore_database
        with pytest.raises(ValueError, match="confirm"):
            restore_database("a.db", "b.db", confirm=False)

    def test_restore_not_found_raises(self, tmp_path):
        from src.backup_database import restore_database
        with pytest.raises(FileNotFoundError):
            restore_database(tmp_path / "nope.db", tmp_path / "target.db", confirm=True)

    def test_restore_overwrites_target(self, tmp_path):
        from src.backup_database import backup_database, restore_database
        db_path = tmp_path / "test.db"
        self._make_db(db_path)
        backup_path = backup_database(db_path, tmp_path / "backups")

        target = tmp_path / "target.db"
        restore_database(backup_path, target, confirm=True)

        conn = sqlite3.connect(str(target))
        row = conn.execute("SELECT val FROM test WHERE id=1").fetchone()
        conn.close()
        assert row[0] == "hello"

    def test_restore_compressed(self, tmp_path):
        from src.backup_database import backup_database, restore_database
        db_path = tmp_path / "test.db"
        self._make_db(db_path)
        backup_path = backup_database(db_path, tmp_path / "backups", compress=True)

        target = tmp_path / "target.db"
        restore_database(backup_path, target, confirm=True)

        conn = sqlite3.connect(str(target))
        row = conn.execute("SELECT val FROM test WHERE id=1").fetchone()
        conn.close()
        assert row[0] == "hello"

    def test_list_backups_empty(self, tmp_path):
        from src.backup_database import list_backups
        result = list_backups(tmp_path / "nonexistent")
        assert result == []

    def test_list_backups_with_files(self, tmp_path):
        from src.backup_database import backup_database, list_backups
        db_path = tmp_path / "test.db"
        self._make_db(db_path)
        backup_dir = tmp_path / "backups"
        p1 = backup_database(db_path, backup_dir)
        p2 = backup_database(db_path, backup_dir)

        backups = list_backups(backup_dir)
        assert len(backups) >= 1
        assert all("path" in b for b in backups)
        assert all("size_bytes" in b for b in backups)

    def test_backup_creates_directory(self, tmp_path):
        from src.backup_database import backup_database
        db_path = tmp_path / "test.db"
        self._make_db(db_path)
        deep_dir = tmp_path / "a" / "b" / "backups"
        result = backup_database(db_path, deep_dir)
        assert result.exists()

    def test_prune_zero_retention(self, tmp_path):
        from src.backup_database import _prune_backups
        count = _prune_backups(tmp_path / "nonexistent", 0)
        assert count == 0
