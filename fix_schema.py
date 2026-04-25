import sys, os
from db.master_pg_connection import get_pg_conn, init_master_pg

# Read .env or whatever we can find to get SUPABASE_DB_URL
url = os.environ.get("SUPABASE_DB_URL")
if not url:
    # try config
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from config.settings import settings
    url = settings.get("supabase_db_url")
    os.environ["SUPABASE_DB_URL"] = url

if not url:
    print("No DB URL")
    sys.exit(1)

init_master_pg(url)

try:
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS media_type TEXT;")
            cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS media_path TEXT;")
        conn.commit()
    print("Columns added successfully.")
except Exception as e:
    print("Error:", e)
