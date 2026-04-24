-- Master database schema: stores tenant registry and superadmin credentials.
-- This database is separate from each tenant's individual whatsbot.db.

CREATE TABLE IF NOT EXISTS tenants (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slug            TEXT    NOT NULL UNIQUE,   -- subdomain: "empresa1"
    name            TEXT    NOT NULL,          -- display name: "Empresa 1 Ltda"
    custom_domain   TEXT    NOT NULL DEFAULT '',-- "chat.empresa1.com" (future)
    status          TEXT    NOT NULL DEFAULT 'active', -- active, suspended, trial
    plan            TEXT    NOT NULL DEFAULT 'free',
    gowa_port       INTEGER NOT NULL UNIQUE,   -- dedicated GOWA port (65001, 65002, ...)
    max_contacts    INTEGER NOT NULL DEFAULT 500,
    openrouter_api_key TEXT NOT NULL DEFAULT '', -- tenant's own key (empty = use global)
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tenants_slug ON tenants(slug);
CREATE INDEX IF NOT EXISTS idx_tenants_status ON tenants(status);

CREATE TABLE IF NOT EXISTS superadmins (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT    NOT NULL UNIQUE,
    password_hash   TEXT    NOT NULL,
    salt            TEXT    NOT NULL,
    created_at      REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS global_config (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_policies (
    tenant_slug     TEXT NOT NULL,
    key             TEXT NOT NULL,
    value           TEXT NOT NULL,
    PRIMARY KEY (tenant_slug, key)
);
