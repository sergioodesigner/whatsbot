"""Master database connection for multi-tenant management.

The master database stores tenant registry, superadmin credentials,
and global SaaS configuration. It is always a single file located at
``data/master.db`` and is independent of tenant databases.
"""

import logging
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_master_path: Path | None = None
_local = threading.local()
_MASTER_SCHEMA = Path(__file__).parent / "master_schema.sql"


def init_master_db(db_path: Path) -> None:
    """Initialize the master database: create tables if needed."""
    global _master_path
    _master_path = db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_master_db()
    conn.executescript(_MASTER_SCHEMA.read_text(encoding="utf-8"))
    conn.commit()
    logger.info("Master database initialized at %s", db_path)


def get_master_db() -> sqlite3.Connection:
    """Return a thread-local connection to the master database."""
    if _master_path is None:
        raise RuntimeError("Master database not initialized. Call init_master_db() first.")
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(_master_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return conn
