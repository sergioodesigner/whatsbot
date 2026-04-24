#!/usr/bin/env python3
"""setup_supabase_buckets.py — Phase 0 / Phase 1 bootstrap script.

Creates the required Supabase Storage buckets and verifies connectivity.
Run this once before activating STORAGE_BACKEND=supabase.

Usage:
    SUPABASE_URL=https://... SUPABASE_SERVICE_ROLE_KEY=... python scripts/setup_supabase_buckets.py

Buckets created:
  - media      (private – stores incoming media files)
  - avatars    (private – stores WhatsApp profile photos)
  - senditems  (private – stores operator-sent attachments)
"""

import os
import sys

REQUIRED_BUCKETS = [
    {"name": "media",     "public": False, "file_size_limit": 50 * 1024 * 1024},
    {"name": "avatars",   "public": False, "file_size_limit": 5 * 1024 * 1024},
    {"name": "senditems", "public": False, "file_size_limit": 50 * 1024 * 1024},
]


def main():
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()

    if not url or not key:
        print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set.", file=sys.stderr)
        sys.exit(1)

    try:
        from supabase import create_client
    except ImportError:
        print("ERROR: supabase-py not installed. Run: pip install supabase", file=sys.stderr)
        sys.exit(1)

    client = create_client(url, key)
    storage = client.storage

    print(f"Connected to Supabase: {url}\n")

    existing = {b["name"] for b in storage.list_buckets()}

    for bucket_def in REQUIRED_BUCKETS:
        name = bucket_def["name"]
        if name in existing:
            print(f"  ✓ Bucket '{name}' already exists – skipping.")
            continue
        try:
            storage.create_bucket(
                name,
                options={
                    "public": bucket_def["public"],
                    "file_size_limit": bucket_def["file_size_limit"],
                },
            )
            print(f"  ✓ Bucket '{name}' created.")
        except Exception as exc:
            print(f"  ✗ Failed to create bucket '{name}': {exc}", file=sys.stderr)
            sys.exit(1)

    print("\nAll buckets are ready.")

    # Quick round-trip test
    test_bucket = REQUIRED_BUCKETS[0]["name"]
    test_key = "__healthcheck__"
    try:
        storage.from_(test_bucket).upload(
            path=test_key,
            file=b"ok",
            file_options={"content-type": "text/plain", "upsert": "true"},
        )
        storage.from_(test_bucket).remove([test_key])
        print(f"Round-trip test on bucket '{test_bucket}': OK")
    except Exception as exc:
        print(f"WARNING: Round-trip test failed: {exc}", file=sys.stderr)

    print("\nPhase 1 storage setup complete.")
    print("Set STORAGE_BACKEND=supabase in Railway env vars to activate.")


if __name__ == "__main__":
    main()
