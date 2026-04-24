#!/usr/bin/env python3
"""setup_supabase_tables.py — Phase 2 bootstrap script.

Creates the required Supabase Postgres tables and verifies connectivity.
Run this once to ensure your SUPABASE_DB_URL is correct and working.

Usage:
    SUPABASE_DB_URL=postgresql://... python scripts/setup_supabase_tables.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Re-use the schema from the connection module
try:
    from db.master_pg_connection import _MASTER_SCHEMA_PG
except ImportError:
    print("Run this from the project root directory.", file=sys.stderr)
    sys.exit(1)

def main():
    url = os.environ.get("SUPABASE_DB_URL", "").strip()

    if not url:
        print("ERROR: SUPABASE_DB_URL must be set.", file=sys.stderr)
        sys.exit(1)

    try:
        import psycopg2
    except ImportError:
        print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary", file=sys.stderr)
        sys.exit(1)

    print(f"Connecting to Postgres...")
    
    try:
        conn = psycopg2.connect(url)
        conn.autocommit = False
        print("✓ Connected successfully.")
    except Exception as exc:
        print(f"✗ Failed to connect: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        with conn.cursor() as cur:
            cur.execute(_MASTER_SCHEMA_PG)
        conn.commit()
        print("✓ Master schema applied (tables created/verified).")
    except Exception as exc:
        conn.rollback()
        print(f"✗ Failed to apply schema: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()

    print("\nPhase 2 Postgres setup complete.")

if __name__ == "__main__":
    main()
