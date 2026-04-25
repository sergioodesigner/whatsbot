import sys, os
from db.master_pg_connection import get_pg_conn
try:
    conn = get_pg_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'messages';")
        rows = cur.fetchall()
        for row in rows:
            print(f"{row[0]}: {row[1]}")
except Exception as e:
    print("Error:", e)
