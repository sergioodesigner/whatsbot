"""Tenant context management using contextvars.

This module provides the mechanism for propagating the current tenant ID
through async/threaded request handling without passing it as a parameter.
"""

import contextvars

# The current tenant's database name (set by middleware, read by get_db())
# Default "default" is used for single-tenant / backward-compatible mode.
current_tenant_db: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_tenant_db", default="default"
)

# The current tenant's slug (for routing, logging, etc.)
current_tenant_slug: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_tenant_slug", default="default"
)
