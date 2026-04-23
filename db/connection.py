"""SQLite connection management with multi-tenant support.

Supports multiple named databases (one per tenant). The active database
is determined by the ``current_tenant_db`` contextvar which is set by
the tenant middleware on each request.

Backward-compatible: ``get_db()`` with no arguments returns the connection
for the *current* tenant (or the "default" database in single-tenant mode).
"""

import logging
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA_FILE = Path(__file__).parent / "schema.sql"


class DatabaseManager:
    """Manages multiple named SQLite databases (one per tenant).

    Each database gets its own thread-local connection pool so that
    concurrent requests for different tenants never share a connection.
    """

    def __init__(self):
        self._databases: dict[str, Path] = {}          # name -> db_path
        self._locals: dict[str, threading.local] = {}  # name -> thread-local storage
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────

    def init(self, name: str, db_path: Path) -> None:
        """Initialize a named database: register it, create tables, run migrations."""
        with self._lock:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._databases[name] = db_path
            self._locals[name] = threading.local()

        conn = self.get(name)
        conn.executescript(_SCHEMA_FILE.read_text(encoding="utf-8"))
        conn.commit()
        _run_migrations(conn)
        logger.info("Database '%s' initialized at %s", name, db_path)

    def get(self, name: str) -> sqlite3.Connection:
        """Return a thread-local connection for the named database."""
        if name not in self._databases:
            raise RuntimeError(
                f"Database '{name}' not initialized. Call db_manager.init() first."
            )
        local = self._locals[name]
        conn = getattr(local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._databases[name]), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = sqlite3.Row
            local.conn = conn
        return conn

    def has(self, name: str) -> bool:
        """Check if a named database is registered."""
        return name in self._databases

    def close_all(self) -> None:
        """Close all connections (for shutdown)."""
        for name in list(self._databases.keys()):
            local = self._locals.get(name)
            if local:
                conn = getattr(local, "conn", None)
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    local.conn = None

    @property
    def names(self) -> list[str]:
        """Return all registered database names."""
        return list(self._databases.keys())


# ── Singleton ─────────────────────────────────────────────────────────

db_manager = DatabaseManager()


# ── Backward-compatible API ───────────────────────────────────────────
# These functions are used by all existing repos (config_repo, contact_repo,
# etc.) and must continue to work in single-tenant mode unchanged.

def init_db(db_path: Path) -> None:
    """Initialize the database (backward-compatible wrapper).

    In single-tenant mode this registers the database as "default".
    """
    db_manager.init("default", db_path)


def get_db() -> sqlite3.Connection:
    """Return the SQLite connection for the current tenant.

    Uses the ``current_tenant_db`` contextvar to determine which database
    to use. Falls back to "default" for single-tenant mode.
    """
    from server.tenant import current_tenant_db

    name = current_tenant_db.get()
    return db_manager.get(name)


# ── Migrations ────────────────────────────────────────────────────────

def _run_migrations(conn: sqlite3.Connection) -> None:
    """Apply incremental schema migrations for existing databases."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(contacts)").fetchall()}
    if "archived_by_app" not in cols:
        conn.execute("ALTER TABLE contacts ADD COLUMN archived_by_app INTEGER NOT NULL DEFAULT 0")
        conn.commit()
        logger.info("Migration: added archived_by_app column to contacts")

    if "can_send" not in cols:
        conn.execute("ALTER TABLE contacts ADD COLUMN can_send INTEGER NOT NULL DEFAULT 1")
        conn.commit()
        logger.info("Migration: added can_send column to contacts")
