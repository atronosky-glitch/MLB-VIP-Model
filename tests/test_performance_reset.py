"""src/performance_reset.py: dry run by default, --apply sets a non-destructive epoch."""

import database.db_manager as dbm
from database.db_manager import get_connection, get_performance_baseline_info, init_db
from src import performance_reset


def _db(tmp_path, monkeypatch):
    path = tmp_path / "reset.db"
    monkeypatch.setattr(dbm, "DB_PATH", path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    init_db(str(path))


def test_dry_run_changes_nothing(tmp_path, monkeypatch, capsys):
    _db(tmp_path, monkeypatch)
    conn = get_connection()
    before = get_performance_baseline_info(conn)
    conn.close()
    assert performance_reset.main(["--reason", "x"]) == 0
    conn = get_connection()
    assert get_performance_baseline_info(conn) == before
    conn.close()
    assert "DRY RUN" in capsys.readouterr().out


def test_apply_sets_epoch_and_reason(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    assert performance_reset.main(["--reason", "Pinnacle-only picks", "--apply"]) == 0
    conn = get_connection()
    info = get_performance_baseline_info(conn)
    conn.close()
    assert info["reason"] == "Pinnacle-only picks" and info["baseline_at"].startswith("20")
