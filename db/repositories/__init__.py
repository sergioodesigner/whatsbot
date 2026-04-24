"""Master-DB repository dispatcher.

Routes calls to either the SQLite or Supabase Postgres backend based on
the ``MASTER_DB_BACKEND`` env var at import time.

Usage (all existing code remains unchanged):

    from db.repositories import tenant_repo
    tenant_repo.list_all()           # works with both backends

    from db.repositories import master_billing_repo
    master_billing_repo.get_profile("slug")

    from db.repositories import master_policy_repo
    master_policy_repo.get_global("key")
"""

from __future__ import annotations

import os as _os


def _master_backend() -> str:
    return _os.environ.get("MASTER_DB_BACKEND", "sqlite").strip().lower()

def _crm_backend() -> str:
    # Phase 3 requires Phase 2 to be active.
    if _master_backend() != "supabase":
        return "sqlite"
    return _os.environ.get("CRM_AUTOMATION_BACKEND", "sqlite").strip().lower()

def _core_backend() -> str:
    # Phase 4 requires Phase 2 to be active.
    if _master_backend() != "supabase":
        return "sqlite"
    return _os.environ.get("CORE_DB_BACKEND", "sqlite").strip().lower()


# ── Lazy module-level proxies ─────────────────────────────────────────
# Each proxy module exposes the same public API as the SQLite version.

class _ModuleProxy:
    """Lazy proxy that selects the right module on first attribute access."""

    def __init__(self, sqlite_mod: str, pg_mod: str, use_crm_backend: bool = False, use_core_backend: bool = False):
        self._sqlite_mod = sqlite_mod
        self._pg_mod = pg_mod
        self._use_crm_backend = use_crm_backend
        self._use_core_backend = use_core_backend
        self._resolved = None

    def _resolve(self):
        if self._resolved is None:
            import importlib
            if self._use_core_backend:
                mod_path = self._pg_mod if _core_backend() == "supabase" else self._sqlite_mod
            elif self._use_crm_backend:
                mod_path = self._pg_mod if _crm_backend() == "supabase" else self._sqlite_mod
            else:
                mod_path = self._pg_mod if _master_backend() == "supabase" else self._sqlite_mod
            self._resolved = importlib.import_module(mod_path)
        return self._resolved

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._resolve(), name)


tenant_repo = _ModuleProxy(
    "db.repositories.tenant_repo",
    "db.repositories.tenant_repo_pg",
)

master_billing_repo = _ModuleProxy(
    "db.repositories.master_billing_repo",
    "db.repositories.master_billing_repo_pg",
)

master_policy_repo = _ModuleProxy(
    "db.repositories.master_policy_repo",
    "db.repositories.master_policy_repo_pg",
)

crm_repo = _ModuleProxy(
    "db.repositories.crm_repo",
    "db.repositories.crm_repo_pg",
    use_crm_backend=True,
)

automation_repo = _ModuleProxy(
    "db.repositories.automation_repo",
    "db.repositories.automation_repo_pg",
    use_crm_backend=True,
)

config_repo = _ModuleProxy(
    "db.repositories.config_repo",
    "db.repositories.config_repo_pg",
    use_core_backend=True,
)

tag_repo = _ModuleProxy(
    "db.repositories.tag_repo",
    "db.repositories.tag_repo_pg",
    use_core_backend=True,
)

contact_repo = _ModuleProxy(
    "db.repositories.contact_repo",
    "db.repositories.contact_repo_pg",
    use_core_backend=True,
)

usage_repo = _ModuleProxy(
    "db.repositories.usage_repo",
    "db.repositories.usage_repo_pg",
    use_core_backend=True,
)
