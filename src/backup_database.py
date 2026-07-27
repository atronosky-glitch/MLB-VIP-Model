"""Database backup and retention management.

Uses the SQLite online backup API for safe live backups, optional
gzip compression, retention-based pruning, and explicit restore with
confirmation gate.
"""

from __future__ import annotations

import gzip
import logging
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def backup_database(
    db_path: str | Path,
    backup_dir: str | Path,
    retention_count: int = 7,
    compress: bool = False,
) -> Path:
    """Create an online backup of the SQLite database.

    Uses the SQLite online backup API so the source database remains
    fully operational during the backup.

    Parameters
    ----------
    db_path:
        Path to the source database file.
    backup_dir:
        Directory where backups are stored.
    retention_count:
        Maximum number of backups to keep (oldest pruned first).
    compress:
        If True, apply gzip compression to the backup file.

    Returns
    -------
    Path to the newly created backup file.
    """
    db_path = Path(db_path)
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    suffix = ".db.gz" if compress else ".db"
    backup_name = f"mlb_backup_{timestamp}{suffix}"
    backup_path = backup_dir / backup_name

    # Source connection for online backup
    src_conn = sqlite3.connect(str(db_path))
    try:
        dest_conn = sqlite3.connect(str(backup_path))
        try:
            src_conn.backup(dest_conn)
            logger.info("Backup created: %s", backup_path)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()

    # Compress if requested (re-compress the already-copied file)
    if compress:
        compressed_path = backup_path.with_suffix(".db.gz")
        with open(backup_path, "rb") as f_in:
            with gzip.open(compressed_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        backup_path.unlink()
        backup_path = compressed_path
        logger.info("Backup compressed: %s", backup_path)

    # Prune old backups
    _prune_backups(backup_dir, retention_count)

    return backup_path


def restore_database(
    backup_path: str | Path,
    db_path: str | Path,
    *,
    confirm: bool = False,
) -> None:
    """Restore a database from a backup file.

    Parameters
    ----------
    backup_path:
        Path to the backup file (.db or .db.gz).
    db_path:
        Path to the target database (will be overwritten).
    confirm:
        Must be True to actually perform the restore. Safety gate.

    Raises
    ------
    ValueError
        If ``confirm`` is not True.
    FileNotFoundError
        If the backup file does not exist.
    """
    if not confirm:
        raise ValueError(
            "Restore requires explicit confirmation (confirm=True). "
            "This will overwrite the target database."
        )

    backup_path = Path(backup_path)
    db_path = Path(db_path)

    if not backup_path.exists():
        raise FileNotFoundError(f"Backup file not found: {backup_path}")

    db_path.parent.mkdir(parents=True, exist_ok=True)

    if backup_path.suffix == ".gz" or str(backup_path).endswith(".db.gz"):
        with gzip.open(backup_path, "rb") as f_in:
            with open(db_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
    else:
        shutil.copy2(backup_path, db_path)

    logger.info("Database restored from %s to %s", backup_path, db_path)


def list_backups(backup_dir: str | Path) -> list[dict[str, object]]:
    """List all backups in a directory, newest first.

    Returns a list of dicts with keys: path, size_bytes, created_at, compressed.
    """
    backup_dir = Path(backup_dir)
    if not backup_dir.exists():
        return []

    backups = []
    for p in sorted(backup_dir.iterdir(), reverse=True):
        if p.name.startswith("mlb_backup_"):
            is_compressed = p.suffix == ".gz"
            backups.append({
                "path": p,
                "size_bytes": p.stat().st_size,
                "created_at": datetime.fromtimestamp(
                    p.stat().st_mtime, tz=timezone.utc
                ),
                "compressed": is_compressed,
            })
    return backups


def _prune_backups(backup_dir: Path, retention_count: int) -> int:
    """Remove oldest backups beyond retention_count. Returns number pruned."""
    if retention_count <= 0:
        return 0

    backups = sorted(
        [p for p in backup_dir.iterdir() if p.name.startswith("mlb_backup_")],
        key=lambda p: p.stat().st_mtime,
    )

    pruned = 0
    while len(backups) > retention_count:
        oldest = backups.pop(0)
        oldest.unlink()
        logger.info("Pruned old backup: %s", oldest.name)
        pruned += 1

    return pruned
