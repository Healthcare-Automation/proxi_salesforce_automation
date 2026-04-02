#!/usr/bin/env python3
"""
Test Supabase (PostgreSQL) connection.
Run from project root: python tests/test_db_connection.py
Requires: DB_PASSWORD in .env, psycopg2-binary installed.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))


def main():
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")

    try:
        from utils.supabase_db import get_connection_string
    except ImportError as e:
        print("FAIL: Could not import supabase_db:", e)
        print("Install: pip install psycopg2-binary python-dotenv")
        sys.exit(1)

    try:
        import psycopg2
    except ImportError:
        print("FAIL: psycopg2 not installed. Run: pip install psycopg2-binary")
        sys.exit(1)

    # Mask password in connection string for display
    conn_str = get_connection_string()
    if "@" in conn_str and ":" in conn_str:
        parts = conn_str.split("@", 1)
        user_part = parts[0]
        if ":" in user_part:
            user_part = user_part.rsplit(":", 1)[0] + ":****"
        conn_str_display = user_part + "@" + parts[1]
    else:
        conn_str_display = conn_str
    print("Connecting:", conn_str_display)

    # Extract host for DNS check and clearer errors
    try:
        host = conn_str.split("@", 1)[1].split("/")[0].split(":")[0]
    except Exception:
        host = None

    if host:
        try:
            import socket
            socket.gethostbyname(host)
        except socket.gaierror as e:
            print("FAIL: Cannot resolve host", repr(host))
            print("  ", e)
            print("  Check: 1) Host in Supabase Dashboard (Settings > Database > URI)")
            print("         2) Internet connection and DNS (e.g. ping supabase.co)")
            sys.exit(1)

    try:
        conn = psycopg2.connect(conn_str)
    except Exception as e:
        err = str(e)
        print("FAIL: Connection error:", e)
        if "translate host name" in err or "nodename nor servname" in err or "getaddrinfo" in err.lower():
            print("  (DNS failed. Verify host in Supabase Dashboard and that you have network access.)")
        sys.exit(1)

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 AS ok;")
            row = cur.fetchone()
            if row and row[0] == 1:
                print("OK: SELECT 1 succeeded.")
            else:
                print("FAIL: Unexpected result:", row)
                sys.exit(1)

            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                ORDER BY table_name;
            """)
            tables = [r[0] for r in cur.fetchall()]
            print("Tables in public schema:", tables if tables else "(none)")
    except Exception as e:
        print("FAIL:", e)
        sys.exit(1)
    finally:
        conn.close()

    print("Connection test passed.")


if __name__ == "__main__":
    main()
