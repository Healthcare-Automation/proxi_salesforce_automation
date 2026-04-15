"""
Supabase (PostgreSQL) logging for scrape runs, email scrapes, and job content.
We INSERT runs and email_scrapes; for job_content we INSERT or UPDATE to keep 1:1 with email_scrapes.

Connection: use DIRECT_URL or DATABASE_URL in .env, or set DB_PASSWORD and optionally
SUPABASE_DB_HOST / SUPABASE_DB_PORT / SUPABASE_DB_USER. Defaults use pooler (ap-northeast-2).
"""

import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

# Optional: only use if psycopg2 is available
try:
    import psycopg2
    from psycopg2 import sql as pg_sql
    from psycopg2.extras import RealDictCursor
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False
    pg_sql = None  # type: ignore


def _validate_pg_identifier(name: str, label: str = "name") -> str:
    """Allow only simple PostgreSQL identifiers (e.g. public, staging, scrape_runs)."""
    import re

    s = (name or "").strip()
    if not s or not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", s):
        raise ValueError(f"Invalid PostgreSQL {label}: {name!r}")
    return s


def _tbl(schema: str, table: str):
    """Qualified Identifier for schema.table (psycopg2.sql)."""
    if not HAS_PSYCOPG2 or pg_sql is None:
        raise RuntimeError("psycopg2 required")
    _validate_pg_identifier(schema, "schema")
    _validate_pg_identifier(table, "table")
    return pg_sql.Identifier(schema, table)

# Default connection params (Supabase pooler/direct for ap-northeast-2; override with env)
SUPABASE_HOST = os.environ.get("SUPABASE_DB_HOST", "aws-1-ap-northeast-2.pooler.supabase.com")
SUPABASE_PORT = int(os.environ.get("SUPABASE_DB_PORT", "5432"))
SUPABASE_DB = os.environ.get("SUPABASE_DB_NAME", "postgres")
SUPABASE_USER = os.environ.get("SUPABASE_DB_USER", "postgres.iyamhwehxsiuvgfherxm")


def get_db_password() -> str:
    """Read DB password from .env (project root)."""
    from dotenv import load_dotenv
    root = Path(__file__).resolve().parent.parent.parent
    load_dotenv(root / ".env")
    return (os.environ.get("DB_PASSWORD") or "").strip()


def get_connection_string() -> str:
    """Build connection string. Uses DIRECT_URL or DATABASE_URL if set (with [YOUR-PASSWORD] replaced by DB_PASSWORD), else host/port/user."""
    from urllib.parse import quote
    from dotenv import load_dotenv
    root = Path(__file__).resolve().parent.parent.parent
    load_dotenv(root / ".env")
    pwd = (os.environ.get("DB_PASSWORD") or "").strip()
    pwd_encoded = quote(pwd, safe="")  # so @ # % etc. don't break the URL
    direct = os.environ.get("DIRECT_URL", "").strip()
    database = os.environ.get("DATABASE_URL", "").strip()
    url = direct or database
    if url and "[YOUR-PASSWORD]" in url and pwd:
        return url.replace("[YOUR-PASSWORD]", pwd_encoded)
    if url and pwd and "postgresql://" in url:
        import re
        if re.search(r"://[^:]+:[^@]+@", url):
            return url
        url = re.sub(r"://([^:]+):[^@]*@", rf"://\1:{pwd_encoded}@", url, count=1)
        return url
    return f"postgresql://{SUPABASE_USER}:{pwd_encoded}@{SUPABASE_HOST}:{SUPABASE_PORT}/{SUPABASE_DB}?sslmode=require"


@contextmanager
def get_conn():
    """Context manager for a DB connection. Yields None if psycopg2 missing or connection fails."""
    if not HAS_PSYCOPG2:
        yield None
        return
    try:
        conn = psycopg2.connect(get_connection_string())
    except Exception as e:
        print(
            f"[proxi_salesforce_automation] Supabase connection failed: {e}",
            file=sys.stderr,
        )
        yield None
        return
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        conn.close()


# Extra columns on job_content / job_current (Salesforce prep + AI placeholders).
# Hyperlink targets: sf_primary_account_id (Account), sf_worksite_account_id (Job_Worksite_Location_1__c).
#
# Logical column order (used in INSERT/UPDATE/SELECT and fresh CREATE TABLE):
#   provenance → identity & location (incl. practice, city, state, address_line) → org/status/POC/dates
#   → parsed description fields (insight, scheduling, clinical incl. types_of_cases, avg_patients_per_day, roster_only) → Salesforce ids
#   → description_full_text (+ raw_columns_json on job_content) → updated/created metadata.
# Existing databases keep their physical column order; code always lists columns explicitly.
JOB_TABLE_EXTRA_TEXT_COLUMNS: tuple[str, ...] = (
    "city",
    "state",
    "address_line",
    "insight",
    "posted_date",
    "dates_needed",
    "standard_schedule",
    "types_of_cases",
    "support_staff",
    "avg_patients_per_day",
    "roster_only",
    "sf_primary_account_id",
    "sf_worksite_account_id",
    "sf_worksite_display_label",
    "sf_job_id",
)


def migrate_job_tables_extra_columns(conn, schema: str = "public") -> None:
    """ALTER TABLE ADD COLUMN IF NOT EXISTS for job_content and job_current (legacy DBs)."""
    if conn is None or not HAS_PSYCOPG2 or pg_sql is None:
        return
    schema = _validate_pg_identifier(schema, "schema")
    jc = _tbl(schema, "job_content")
    jcur = _tbl(schema, "job_current")
    with conn.cursor() as cur:
        for col in JOB_TABLE_EXTRA_TEXT_COLUMNS:
            cur.execute(
                pg_sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} TEXT;").format(
                    jc, pg_sql.Identifier(col)
                )
            )
            cur.execute(
                pg_sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} TEXT;").format(
                    jcur, pg_sql.Identifier(col)
                )
            )
        for col_drop in ("content_file", "other_notes"):
            for tbl_name in ("job_content", "job_current"):
                cur.execute(
                    pg_sql.SQL("ALTER TABLE {} DROP COLUMN IF EXISTS {};").format(
                        _tbl(schema, tbl_name), pg_sql.Identifier(col_drop)
                    )
                )
        for tbl_name in ("job_content", "job_current"):
            cur.execute(
                pg_sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} TEXT;").format(
                    _tbl(schema, tbl_name), pg_sql.Identifier("view_job_link")
                )
            )
        cur.execute(
            pg_sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} TEXT;").format(
                jc, pg_sql.Identifier("scrape_change_summary")
            )
        )
        # Legacy: required_procedures + additional_requirements → types_of_cases, then drop old cols.
        for tbl_name in ("job_content", "job_current"):
            cur.execute(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s AND column_name = 'required_procedures'
                LIMIT 1;
                """,
                (schema, tbl_name),
            )
            if not cur.fetchone():
                continue
            tbl = _tbl(schema, tbl_name)
            cur.execute(
                pg_sql.SQL(
                    """
                    UPDATE {} SET types_of_cases = NULLIF(
                        TRIM(BOTH FROM CONCAT_WS(E'\\n',
                            NULLIF(TRIM(BOTH FROM COALESCE(required_procedures, '')), ''),
                            NULLIF(TRIM(BOTH FROM COALESCE(additional_requirements, '')), '')
                        )),
                        ''
                    )
                    WHERE types_of_cases IS NULL
                       OR TRIM(BOTH FROM types_of_cases) = '';
                    """
                ).format(tbl)
            )
            for legacy in ("required_procedures", "additional_requirements"):
                cur.execute(
                    pg_sql.SQL("ALTER TABLE {} DROP COLUMN IF EXISTS {};").format(
                        tbl, pg_sql.Identifier(legacy)
                    )
                )
    conn.commit()


def _ensure_sf_mapping_tables(conn, schema: str = "public") -> None:
    """sf_account_reference — static Salesforce Account Ids (primary org, default worksite fallback label, etc.)."""
    if conn is None or not HAS_PSYCOPG2 or pg_sql is None:
        return
    schema = _validate_pg_identifier(schema, "schema")
    ref = _tbl(schema, "sf_account_reference")
    wmap = _tbl(schema, "sf_worksite_location_map")
    with conn.cursor() as cur:
        cur.execute(
            pg_sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {} (
                    id SERIAL PRIMARY KEY,
                    reference_key TEXT NOT NULL UNIQUE,
                    salesforce_account_id TEXT NOT NULL,
                    account_name TEXT,
                    salesforce_field_hint TEXT,
                    notes TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
                """
            ).format(ref)
        )
        cur.execute(
            pg_sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {} (
                    id SERIAL PRIMARY KEY,
                    location_key TEXT NOT NULL UNIQUE,
                    city TEXT,
                    state TEXT,
                    salesforce_account_id TEXT NOT NULL,
                    display_label TEXT,
                    source TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
                """
            ).format(wmap)
        )
        # Existing DBs (e.g. older staging) may already have this table without city/state;
        # CREATE TABLE IF NOT EXISTS does not add new columns, so migrate before indexing.
        for alter_tail in (
            "ADD COLUMN IF NOT EXISTS location_key TEXT",
            "ADD COLUMN IF NOT EXISTS city TEXT",
            "ADD COLUMN IF NOT EXISTS state TEXT",
            "ADD COLUMN IF NOT EXISTS salesforce_account_id TEXT",
            "ADD COLUMN IF NOT EXISTS display_label TEXT",
            "ADD COLUMN IF NOT EXISTS source TEXT",
            "ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()",
            "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()",
        ):
            cur.execute(pg_sql.SQL("ALTER TABLE {} " + alter_tail + ";").format(wmap))
        cur.execute(
            pg_sql.SQL(
                "CREATE INDEX IF NOT EXISTS sf_worksite_location_map_city_state ON {} (city, state);"
            ).format(wmap)
        )
    conn.commit()


def _normalize_location_key(city: str, state: str) -> str:
    c = (city or "").strip().lower()
    s = (state or "").strip().upper()
    c = " ".join(c.split())
    s = " ".join(s.split())
    if not c and not s:
        return ""
    return f"{c}|{s}"


def fetch_worksite_account_id_for_location(
    conn,
    city: str,
    state: str,
    *,
    schema: str = "public",
) -> Optional[str]:
    """Return mapped Salesforce Account Id for (city,state), if any."""
    if conn is None or not HAS_PSYCOPG2 or pg_sql is None:
        return None
    schema = _validate_pg_identifier(schema, "schema")
    key = _normalize_location_key(city, state)
    if not key:
        return None
    wmap = _tbl(schema, "sf_worksite_location_map")
    with conn.cursor() as cur:
        cur.execute(
            pg_sql.SQL(
                "SELECT salesforce_account_id FROM {} WHERE location_key = %s LIMIT 1;"
            ).format(wmap),
            (key,),
        )
        row = cur.fetchone()
    return str(row[0]).strip() if row and row[0] else None


def upsert_worksite_account_id_for_location(
    conn,
    city: str,
    state: str,
    *,
    salesforce_account_id: str,
    display_label: Optional[str] = None,
    source: Optional[str] = None,
    schema: str = "public",
) -> None:
    """Insert/update the location→AccountId mapping row."""
    if conn is None or not HAS_PSYCOPG2 or pg_sql is None:
        return
    schema = _validate_pg_identifier(schema, "schema")
    key = _normalize_location_key(city, state)
    sid = (salesforce_account_id or "").strip()
    if not key or not sid:
        return
    wmap = _tbl(schema, "sf_worksite_location_map")
    old_display = pg_sql.Composed([wmap, pg_sql.SQL(".display_label")])
    old_source = pg_sql.Composed([wmap, pg_sql.SQL(".source")])
    with conn.cursor() as cur:
        cur.execute(
            pg_sql.SQL(
                """
                INSERT INTO {} (
                    location_key, city, state, salesforce_account_id, display_label, source, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (location_key) DO UPDATE SET
                    city = EXCLUDED.city,
                    state = EXCLUDED.state,
                    salesforce_account_id = EXCLUDED.salesforce_account_id,
                    display_label = COALESCE(EXCLUDED.display_label, {}),
                    source = COALESCE(EXCLUDED.source, {}),
                    updated_at = NOW();
                """
            ).format(wmap, old_display, old_source),
            (
                key,
                (city or "").strip() or None,
                (state or "").strip() or None,
                sid,
                (display_label or "").strip() or None,
                (source or "").strip() or None,
            ),
        )
    conn.commit()


def delete_worksite_location_map_for_location(
    conn,
    city: str,
    state: str,
    *,
    schema: str = "public",
) -> int:
    """
    Remove the ``sf_worksite_location_map`` row for normalized (city, state).

    Used when Salesforce returns **entity is deleted** for a worksite Account Id still stored
    in the map so the next create can POST a new Account and upsert a fresh Id.
    """
    if conn is None or not HAS_PSYCOPG2 or pg_sql is None:
        return 0
    schema = _validate_pg_identifier(schema, "schema")
    key = _normalize_location_key(city, state)
    if not key:
        return 0
    wmap = _tbl(schema, "sf_worksite_location_map")
    with conn.cursor() as cur:
        cur.execute(pg_sql.SQL("DELETE FROM {} WHERE location_key = %s;").format(wmap), (key,))
        return int(cur.rowcount or 0)


def delete_worksite_location_map_by_salesforce_account_id(
    conn,
    salesforce_account_id: str,
    *,
    schema: str = "public",
) -> int:
    """Delete any map row pointing at a Salesforce Account Id (e.g. after SF admin deleted the Account)."""
    if conn is None or not HAS_PSYCOPG2 or pg_sql is None:
        return 0
    sid = (salesforce_account_id or "").strip()
    if not sid:
        return 0
    schema = _validate_pg_identifier(schema, "schema")
    wmap = _tbl(schema, "sf_worksite_location_map")
    with conn.cursor() as cur:
        cur.execute(
            pg_sql.SQL("DELETE FROM {} WHERE salesforce_account_id = %s;").format(wmap),
            (sid,),
        )
        return int(cur.rowcount or 0)


def delete_all_records_for_job_id(
    conn,
    job_id: str,
    *,
    schema: str = "public",
    dry_run: bool = False,
) -> dict[str, int]:
    """
    Remove all pipeline rows tied to a Kimedics ``job_id`` (same value as ``job_post_id`` in emails).

    Deletes (in order respecting FKs):

    - ``job_event_log`` rows for this ``job_id``
    - Clears ``job_current.last_job_content_id``, then ``job_content``, then ``job_current``
    - ``email_scrapes`` with ``job_post_id`` = ``job_id``
    - ``scrape_runs`` that end up with no remaining ``email_scrapes``, ``job_content``, or ``job_event_log`` rows

    Does **not** delete ``sf_worksite_location_map`` or ``sf_account_reference`` (not keyed by job).

    Returns per-step row counts (``dry_run`` uses SELECT counts only; no mutations).
    """
    if conn is None or not HAS_PSYCOPG2 or pg_sql is None:
        raise RuntimeError("psycopg2 connection required")
    jid = (job_id or "").strip()
    if not jid:
        raise ValueError("job_id must be non-empty")
    schema = _validate_pg_identifier(schema, "schema")
    jc = _tbl(schema, "job_content")
    jcur = _tbl(schema, "job_current")
    jel = _tbl(schema, "job_event_log")
    es = _tbl(schema, "email_scrapes")
    sr = _tbl(schema, "scrape_runs")

    stats: dict[str, int] = {
        "job_event_log": 0,
        "job_current_last_content_nulled": 0,
        "job_content": 0,
        "job_current": 0,
        "email_scrapes": 0,
        "scrape_runs_orphans": 0,
    }

    with conn.cursor() as cur:
        cur.execute(
            pg_sql.SQL(
                """
                SELECT DISTINCT run_id FROM {} WHERE run_id IS NOT NULL AND (job_id = %s OR job_post_id = %s)
                UNION
                SELECT DISTINCT run_id FROM {} WHERE run_id IS NOT NULL AND job_post_id = %s;
                """
            ).format(jc, es),
            (jid, jid, jid),
        )
        run_ids = sorted({int(r[0]) for r in cur.fetchall() if r[0] is not None})

        cur.execute(
            pg_sql.SQL("SELECT COUNT(*) FROM {} WHERE job_id = %s;").format(jel),
            (jid,),
        )
        stats["job_event_log"] = int(cur.fetchone()[0])

        cur.execute(
            pg_sql.SQL(
                "SELECT COUNT(*) FROM {} WHERE job_id = %s AND last_job_content_id IS NOT NULL;"
            ).format(jcur),
            (jid,),
        )
        stats["job_current_last_content_nulled"] = int(cur.fetchone()[0])

        cur.execute(
            pg_sql.SQL(
                "SELECT COUNT(*) FROM {} WHERE job_id = %s OR job_post_id = %s;"
            ).format(jc),
            (jid, jid),
        )
        stats["job_content"] = int(cur.fetchone()[0])

        cur.execute(
            pg_sql.SQL("SELECT COUNT(*) FROM {} WHERE job_id = %s;").format(jcur),
            (jid,),
        )
        stats["job_current"] = int(cur.fetchone()[0])

        cur.execute(
            pg_sql.SQL("SELECT COUNT(*) FROM {} WHERE job_post_id = %s;").format(es),
            (jid,),
        )
        stats["email_scrapes"] = int(cur.fetchone()[0])

        # After this job's rows would be removed, these runs would have no remaining children.
        orphan_runs = 0
        for rid in run_ids:
            cur.execute(
                pg_sql.SQL(
                    "SELECT COUNT(*) FROM {} WHERE run_id = %s AND NOT (job_post_id IS NOT DISTINCT FROM %s);"
                ).format(es),
                (rid, jid),
            )
            n_es = int(cur.fetchone()[0])
            cur.execute(
                pg_sql.SQL(
                    "SELECT COUNT(*) FROM {} WHERE run_id = %s "
                    "AND NOT (job_id IS NOT DISTINCT FROM %s OR job_post_id IS NOT DISTINCT FROM %s);"
                ).format(jc),
                (rid, jid, jid),
            )
            n_jc = int(cur.fetchone()[0])
            cur.execute(
                pg_sql.SQL(
                    "SELECT COUNT(*) FROM {} WHERE run_id = %s AND NOT (job_id IS NOT DISTINCT FROM %s);"
                ).format(jel),
                (rid, jid),
            )
            n_jel = int(cur.fetchone()[0])
            if n_es == 0 and n_jc == 0 and n_jel == 0:
                orphan_runs += 1
        stats["scrape_runs_orphans"] = orphan_runs

    if dry_run:
        return stats

    with conn.cursor() as cur:
        cur.execute(pg_sql.SQL("DELETE FROM {} WHERE job_id = %s;").format(jel), (jid,))

        cur.execute(
            pg_sql.SQL("UPDATE {} SET last_job_content_id = NULL WHERE job_id = %s;").format(jcur),
            (jid,),
        )

        cur.execute(
            pg_sql.SQL("DELETE FROM {} WHERE job_id = %s OR job_post_id = %s;").format(jc),
            (jid, jid),
        )

        cur.execute(pg_sql.SQL("DELETE FROM {} WHERE job_id = %s;").format(jcur), (jid,))

        cur.execute(
            pg_sql.SQL("DELETE FROM {} WHERE job_post_id = %s;").format(es),
            (jid,),
        )

        deleted_runs = 0
        for rid in run_ids:
            cur.execute(
                pg_sql.SQL("SELECT COUNT(*) FROM {} WHERE run_id = %s;").format(es),
                (rid,),
            )
            n_es = int(cur.fetchone()[0])
            cur.execute(
                pg_sql.SQL("SELECT COUNT(*) FROM {} WHERE run_id = %s;").format(jc),
                (rid,),
            )
            n_jc = int(cur.fetchone()[0])
            cur.execute(
                pg_sql.SQL("SELECT COUNT(*) FROM {} WHERE run_id = %s;").format(jel),
                (rid,),
            )
            n_jel = int(cur.fetchone()[0])
            if n_es == 0 and n_jc == 0 and n_jel == 0:
                cur.execute(pg_sql.SQL("DELETE FROM {} WHERE id = %s;").format(sr), (rid,))
                deleted_runs += int(cur.rowcount or 0)
        stats["scrape_runs_orphans"] = deleted_runs

    return stats


def _ensure_job_event_log_table(conn, schema: str = "public") -> None:
    """
    Append-only audit log for pipeline decisions + field updates (per job_id).

    This is intentionally separate from job_content (scraped snapshots) and job_current (latest state).
    """
    if conn is None or not HAS_PSYCOPG2 or pg_sql is None:
        return
    schema = _validate_pg_identifier(schema, "schema")
    jel = _tbl(schema, "job_event_log")
    sr = _tbl(schema, "scrape_runs")
    with conn.cursor() as cur:
        cur.execute(
            pg_sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {} (
                    id SERIAL PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    run_id INTEGER REFERENCES {}(id),
                    payload JSONB,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
                """
            ).format(jel, sr)
        )
        cur.execute(
            pg_sql.SQL("ALTER TABLE {} DROP COLUMN IF EXISTS job_content_id;").format(jel)
        )
        cur.execute(
            pg_sql.SQL(
                "CREATE INDEX IF NOT EXISTS job_event_log_job_id_created_at ON {} (job_id, created_at);"
            ).format(jel)
        )
        cur.execute(
            pg_sql.SQL(
                "CREATE INDEX IF NOT EXISTS job_event_log_event_type_created_at ON {} (event_type, created_at);"
            ).format(jel)
        )
    conn.commit()


def seed_sf_account_reference_defaults(conn, schema: str = "public") -> None:
    """
    Seed known Aspen hyperlink Account Ids (iterative: add rows as you discover more).
    Does not overwrite existing keys (ON CONFLICT DO NOTHING).
    """
    if conn is None or not HAS_PSYCOPG2 or pg_sql is None:
        return
    schema = _validate_pg_identifier(schema, "schema")
    ref = _tbl(schema, "sf_account_reference")
    rows = [
        (
            "primary_aspen_dental_management",
            "0015f00000HH63kAAD",
            "Aspen Dental Management Inc.",
            "Account (hyperlink)",
            "Default org Account for new job posts.",
        ),
        (
            "worksite_location_default",
            "001UP00000HKOXRYA5",
            "Worksite default (Job_Worksite_Location_1__c)",
            "Job_Worksite_Location_1__c (hyperlink)",
            "Optional reference row only — not applied by payload builders; real worksite Id must live on the job row or be resolved via enrichment.",
        ),
    ]
    with conn.cursor() as cur:
        for key, sid, name, hint, notes in rows:
            cur.execute(
                pg_sql.SQL(
                    """
                    INSERT INTO {} (reference_key, salesforce_account_id, account_name, salesforce_field_hint, notes)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (reference_key) DO NOTHING;
                    """
                ).format(ref),
                (key, sid, name, hint, notes),
            )
    conn.commit()


def ensure_full_staging_schema(
    conn,
    schema: str = "staging",
    *,
    seed_sf_defaults: bool = True,
) -> None:
    """
    Staging mirror of production email + job tables + Salesforce lookup tables.
    Same table and column names as public; only schema differs.
    """
    if conn is None or not HAS_PSYCOPG2 or pg_sql is None:
        return
    schema = _validate_pg_identifier(schema, "schema")
    ensure_email_tables(conn, schema)
    _create_job_content_table(conn, schema)
    _create_job_current_table(conn, schema)
    migrate_job_tables_extra_columns(conn, schema)
    _ensure_sf_mapping_tables(conn, schema)
    _ensure_job_event_log_table(conn, schema)
    if seed_sf_defaults:
        seed_sf_account_reference_defaults(conn, schema)


def ensure_schema_for_writes(conn, schema: str = "public") -> None:
    """
    Ensure tables exist for the target schema before local writes.
    public -> ensure_tables (production); any other schema -> full staging mirror DDL.
    """
    if conn is None:
        return
    schema = _validate_pg_identifier(schema, "schema")
    if schema == "public":
        ensure_tables(conn)
    else:
        ensure_full_staging_schema(conn, schema=schema, seed_sf_defaults=True)


def _create_job_content_table(conn, schema: str) -> None:
    if conn is None or not HAS_PSYCOPG2 or pg_sql is None:
        return
    schema = _validate_pg_identifier(schema, "schema")
    sr = _tbl(schema, "scrape_runs")
    es = _tbl(schema, "email_scrapes")
    jc = _tbl(schema, "job_content")
    with conn.cursor() as cur:
        cur.execute(
            pg_sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {} (
                    id SERIAL PRIMARY KEY,
                    run_id INTEGER NOT NULL REFERENCES {}(id),
                    email_scrape_id INTEGER UNIQUE REFERENCES {}(id),
                    job_post_id TEXT NOT NULL,
                    email_received_date DATE,
                    view_job_link TEXT,
                    job_id TEXT,
                    title_line TEXT,
                    location_line TEXT,
                    practice_value TEXT,
                    city TEXT,
                    state TEXT,
                    address_line TEXT,
                    job_title TEXT,
                    posting_org TEXT,
                    priority TEXT,
                    status TEXT,
                    point_of_contact TEXT,
                    provider_start_date TEXT,
                    provider_end_date TEXT,
                    posted_date TEXT,
                    insight TEXT,
                    dates_needed TEXT,
                    standard_schedule TEXT,
                    types_of_cases TEXT,
                    support_staff TEXT,
                    avg_patients_per_day TEXT,
                    roster_only TEXT,
                    sf_primary_account_id TEXT,
                    sf_worksite_account_id TEXT,
                    sf_worksite_display_label TEXT,
                    sf_job_id TEXT,
                    description_full_text TEXT,
                    raw_columns_json JSONB,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
                """
            ).format(jc, sr, es)
        )
    conn.commit()


def _create_job_current_table(conn, schema: str) -> None:
    if conn is None or not HAS_PSYCOPG2 or pg_sql is None:
        return
    schema = _validate_pg_identifier(schema, "schema")
    jc = _tbl(schema, "job_content")
    jcur = _tbl(schema, "job_current")
    with conn.cursor() as cur:
        cur.execute(
            pg_sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {} (
                    job_id TEXT PRIMARY KEY,
                    title_line TEXT,
                    location_line TEXT,
                    practice_value TEXT,
                    city TEXT,
                    state TEXT,
                    address_line TEXT,
                    job_title TEXT,
                    posting_org TEXT,
                    priority TEXT,
                    status TEXT,
                    point_of_contact TEXT,
                    provider_start_date TEXT,
                    provider_end_date TEXT,
                    posted_date TEXT,
                    view_job_link TEXT,
                    insight TEXT,
                    dates_needed TEXT,
                    standard_schedule TEXT,
                    types_of_cases TEXT,
                    support_staff TEXT,
                    avg_patients_per_day TEXT,
                    roster_only TEXT,
                    sf_primary_account_id TEXT,
                    sf_worksite_account_id TEXT,
                    sf_worksite_display_label TEXT,
                    sf_job_id TEXT,
                    description_full_text TEXT,
                    last_job_content_id INTEGER REFERENCES {}(id),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
                """
            ).format(jcur, jc)
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Schema: scrape_runs, email_scrapes, job_content (content linked to email via email_scrape_id)
# ---------------------------------------------------------------------------
#
# Logging flow (1:1 email_scrapes <-> job_content):
#
# 1. Gmail scrape (src/local/local_run_scrape_gmail.py):
#    - Inserts one scrape_run (run_type='gmail'), then one email_scrapes row per parsed email.
#    - Same job_id can appear in multiple emails (e.g. "New job post", "Status update") → multiple
#      email_scrapes rows with the same job_post_id but different date (and id).
#
# 2. Link batch (tests/scrape_kimedics_batch_playwright.py or Modal Playwright):
#    - Reads job_emails.csv (one row per email, with job_post_id and date).
#    - For each row: scrapes the job URL, then inserts/updates job_content.
#    - email_scrape_id is found by (job_post_id, date) using FULL timestamp so each email row
#      maps to exactly one email_scrapes.id.
#    - At most one job_content row per email_scrape_id (insert new row or update the existing row
#      for that email — keyed by email_scrape_id).
#
# Result: each email_scrapes row has at most one job_content row; each job_content row has
# exactly one email_scrape_id (when linked). So 1:1 between the two tables.
#
# 3. Current state (not a log): job_current
#    - One row per job_id: the latest scraped state for that job.
#    - Updated whenever we insert/update job_content (link batch). Use this table for
#      "current state of each job" (e.g. dashboards, exports).
# ---------------------------------------------------------------------------


def ensure_tables(conn) -> None:
    """Create tables if they do not exist. Safe to call on every run."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS scrape_runs (
                id SERIAL PRIMARY KEY,
                run_type TEXT NOT NULL,
                started_at TIMESTAMPTZ NOT NULL,
                finished_at TIMESTAMPTZ,
                columns_json JSONB,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS email_scrapes (
                id SERIAL PRIMARY KEY,
                run_id INTEGER NOT NULL REFERENCES scrape_runs(id),
                job_post_id TEXT,
                location TEXT,
                action_or_change TEXT,
                view_job_link TEXT,
                subject TEXT,
                date TIMESTAMPTZ,
                from_ TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS job_content (
                id SERIAL PRIMARY KEY,
                run_id INTEGER NOT NULL REFERENCES scrape_runs(id),
                email_scrape_id INTEGER UNIQUE REFERENCES email_scrapes(id),
                job_post_id TEXT NOT NULL,
                email_received_date DATE,
                view_job_link TEXT,
                job_id TEXT,
                title_line TEXT,
                location_line TEXT,
                practice_value TEXT,
                city TEXT,
                state TEXT,
                address_line TEXT,
                job_title TEXT,
                posting_org TEXT,
                priority TEXT,
                status TEXT,
                point_of_contact TEXT,
                provider_start_date TEXT,
                provider_end_date TEXT,
                posted_date TEXT,
                insight TEXT,
                dates_needed TEXT,
                standard_schedule TEXT,
                types_of_cases TEXT,
                support_staff TEXT,
                avg_patients_per_day TEXT,
                roster_only TEXT,
                sf_primary_account_id TEXT,
                sf_worksite_account_id TEXT,
                sf_worksite_display_label TEXT,
                sf_job_id TEXT,
                description_full_text TEXT,
                raw_columns_json JSONB,
                scrape_change_summary TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS job_current (
                job_id TEXT PRIMARY KEY,
                title_line TEXT,
                location_line TEXT,
                practice_value TEXT,
                city TEXT,
                state TEXT,
                address_line TEXT,
                job_title TEXT,
                posting_org TEXT,
                priority TEXT,
                status TEXT,
                point_of_contact TEXT,
                provider_start_date TEXT,
                provider_end_date TEXT,
                posted_date TEXT,
                view_job_link TEXT,
                insight TEXT,
                dates_needed TEXT,
                standard_schedule TEXT,
                types_of_cases TEXT,
                support_staff TEXT,
                avg_patients_per_day TEXT,
                roster_only TEXT,
                sf_primary_account_id TEXT,
                sf_worksite_account_id TEXT,
                sf_worksite_display_label TEXT,
                sf_job_id TEXT,
                description_full_text TEXT,
                last_job_content_id INTEGER REFERENCES job_content(id),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS job_event_log (
                id SERIAL PRIMARY KEY,
                job_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                run_id INTEGER REFERENCES scrape_runs(id),
                payload JSONB,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        cur.execute("ALTER TABLE job_event_log DROP COLUMN IF EXISTS job_content_id;")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS job_event_log_job_id_created_at ON job_event_log (job_id, created_at);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS job_event_log_event_type_created_at ON job_event_log (event_type, created_at);"
        )
    conn.commit()
    migrate_job_tables_extra_columns(conn, "public")
    _ensure_sf_mapping_tables(conn, "public")
    _ensure_job_event_log_table(conn, "public")
    seed_sf_account_reference_defaults(conn, "public")


def ensure_email_tables(conn, schema: str = "public") -> None:
    """
    Create scrape_runs and email_scrapes in the given schema if missing (same columns as public).
    Use for staging or other mirrors; does not create job_content / job_current.
    """
    if conn is None or not HAS_PSYCOPG2 or pg_sql is None:
        return
    schema = _validate_pg_identifier(schema, "schema")
    sr = _tbl(schema, "scrape_runs")
    es = _tbl(schema, "email_scrapes")
    with conn.cursor() as cur:
        cur.execute(
            pg_sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {} (
                    id SERIAL PRIMARY KEY,
                    run_type TEXT NOT NULL,
                    started_at TIMESTAMPTZ NOT NULL,
                    finished_at TIMESTAMPTZ,
                    columns_json JSONB,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
                """
            ).format(sr)
        )
        cur.execute(
            pg_sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {} (
                    id SERIAL PRIMARY KEY,
                    run_id INTEGER NOT NULL REFERENCES {}(id),
                    job_post_id TEXT,
                    location TEXT,
                    action_or_change TEXT,
                    view_job_link TEXT,
                    subject TEXT,
                    date TIMESTAMPTZ,
                    from_ TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
                """
            ).format(es, sr)
        )
    conn.commit()


def truncate_email_scrape_tables(
    conn, schema: str, *, allow_public: bool = False
) -> None:
    """
    TRUNCATE scrape_runs RESTART IDENTITY CASCADE in ``schema``.

    Clears dependent rows: email_scrapes, job_content (FKs), and job_current (FK to job_content).
    Does not touch sf_account_reference.

    For schema ``public``, callers must pass ``allow_public=True`` (e.g. after ``--production-ok``)
    or set env ``ALLOW_TRUNCATE_PUBLIC_EMAIL=1``.
    """
    if conn is None or not HAS_PSYCOPG2 or pg_sql is None:
        return
    raw = schema.strip().lower()
    if raw == "public" and not allow_public and os.environ.get("ALLOW_TRUNCATE_PUBLIC_EMAIL") != "1":
        raise ValueError(
            "Refusing to TRUNCATE public scrape pipeline tables. "
            "Pass allow_public=True (e.g. bulk Gmail with --production-ok), "
            "or set ALLOW_TRUNCATE_PUBLIC_EMAIL=1, or use a non-public schema."
        )
    schema = _validate_pg_identifier(schema, "schema")
    sr = _tbl(schema, "scrape_runs")
    with conn.cursor() as cur:
        cur.execute(
            pg_sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE;").format(sr)
        )
    conn.commit()


def log_run_start(conn, run_type: str, columns: list[str], *, schema: str = "public") -> Optional[int]:
    """Insert a scrape run (started). Returns run id or None."""
    if conn is None:
        return None
    schema = _validate_pg_identifier(schema, "schema")
    sr = _tbl(schema, "scrape_runs")
    with conn.cursor() as cur:
        cur.execute(
            pg_sql.SQL(
                """
                INSERT INTO {} (run_type, started_at, columns_json)
                VALUES (%s, %s, %s)
                RETURNING id;
                """
            ).format(sr),
            (run_type, datetime.now(timezone.utc), json.dumps(columns)),
        )
        row = cur.fetchone()
        return row[0] if row else None


def log_run_finish(conn, run_id: int, *, schema: str = "public") -> None:
    """Set finished_at for a run."""
    if conn is None or run_id is None:
        return
    schema = _validate_pg_identifier(schema, "schema")
    sr = _tbl(schema, "scrape_runs")
    with conn.cursor() as cur:
        cur.execute(
            pg_sql.SQL("UPDATE {} SET finished_at = %s WHERE id = %s;").format(sr),
            (datetime.now(timezone.utc), run_id),
        )
    conn.commit()


def log_job_event(
    conn,
    *,
    job_id: str,
    event_type: str,
    payload: Optional[dict] = None,
    run_id: Optional[int] = None,
    schema: str = "public",
) -> Optional[int]:
    """
    Append one audit event for a job_id.

    This is the canonical place to log: mapping outcomes, SF API calls, field diffs, and errors.
    Join to job_current on job_id to look up the full job state.
    """
    if conn is None or not HAS_PSYCOPG2 or pg_sql is None:
        return None
    jid = (job_id or "").strip()
    et = (event_type or "").strip()
    if not jid or not et:
        return None
    schema = _validate_pg_identifier(schema, "schema")
    jel = _tbl(schema, "job_event_log")
    with conn.cursor() as cur:
        cur.execute(
            pg_sql.SQL(
                """
                INSERT INTO {} (job_id, event_type, run_id, payload)
                VALUES (%s, %s, %s, %s)
                RETURNING id;
                """
            ).format(jel),
            (
                jid,
                et,
                int(run_id) if run_id is not None else None,
                json.dumps(payload) if payload is not None else None,
            ),
        )
        row = cur.fetchone()
    return int(row[0]) if row else None


def _diff_dict_keys(prev: Optional[dict], nxt: Optional[dict]) -> list[str]:
    """Return keys whose values differ (shallow compare)."""
    a = prev or {}
    b = nxt or {}
    keys = set(a.keys()) | set(b.keys())
    changed: list[str] = []
    for k in sorted(keys):
        if a.get(k) != b.get(k):
            changed.append(str(k))
    return changed


def get_existing_email_keys(
    conn, since_hours_ago: float = 2.0, *, schema: str = "public"
) -> set[tuple[str, str]]:
    """
    Return set of (job_post_id, date_key) for emails already in email_scrapes.
    date_key is date truncated to second in ISO format for exact matching.
    Use since_hours_ago to limit how far back we look (e.g. 2 so we overlap the 1h window).
    """
    if conn is None:
        return set()
    schema = _validate_pg_identifier(schema, "schema")
    es = _tbl(schema, "email_scrapes")
    with conn.cursor() as cur:
        cur.execute(
            pg_sql.SQL(
                """
                SELECT job_post_id, to_char(date_trunc('second', date), 'YYYY-MM-DD"T"HH24:MI:SS".000000+00"') AS date_key
                FROM {}
                WHERE date >= NOW() - INTERVAL '1 hour' * %s
                  AND job_post_id IS NOT NULL AND job_post_id != '';
                """
            ).format(es),
            (since_hours_ago,),
        )
        rows = cur.fetchall()
    out = set()
    for (jid, dkey) in rows:
        if jid and dkey:
            jid = str(jid).strip()
            dkey = _normalize_email_date_key(str(dkey))
            if dkey:
                out.add((jid, dkey))
    return out


def _normalize_email_date_key(date_iso: Optional[str]) -> str:
    """Normalize email date to YYYY-MM-DDTHH:MM:SS+00:00 for exact matching."""
    if not date_iso or not isinstance(date_iso, str):
        return ""
    s = date_iso.strip().replace("Z", "+00:00")
    if "." in s:
        s = s.split(".")[0] + ("+" + s.split("+")[1] if "+" in s else "")
    if "T" in s and len(s) >= 19:
        return s[:19] + "+00:00"
    if len(s) >= 10:
        return s[:10] + "T00:00:00+00:00"
    return ""


def filter_parsed_emails_not_logged(
    parsed_rows: list[dict],
    existing_keys: set[tuple[str, str]],
) -> list[dict]:
    """
    Return only parsed email rows that are not already in Supabase (by job_post_id + date).
    existing_keys is from get_existing_email_keys(conn, since_hours_ago=2).
    Ensures we never skip an email that hasn't been logged.
    """
    out = []
    for r in parsed_rows:
        jid = (r.get("job_post_id") or "").strip()
        date_key = _normalize_email_date_key(r.get("date"))
        if not jid:
            continue
        if (jid, date_key) in existing_keys:
            continue
        out.append(r)
    return out


def sync_email_scrape_locations_from_parsed(
    conn,
    parsed_rows: list[dict],
    *,
    dry_run: bool = False,
    schema: str = "public",
) -> dict:
    """
    Re-apply parsed Gmail rows to Supabase: UPDATE email_scrapes.location only.
    Rows match on job_post_id and email date (truncated to seconds), same as find_email_scrape_id.
    Does not modify created_at, run_id, or any other column.

    parsed_rows: output of parse_kimedics_job_email (needs job_post_id, date, location).

    Returns stats: candidates (unique job+date keys), rows_updated (or would_change if dry_run),
    rows_unchanged_or_missing (UPDATE rowcount 0 — already correct or no DB row; best-effort count in dry_run).
    """
    if conn is None:
        return {"error": "no connection", "candidates": 0, "rows_updated": 0}

    schema = _validate_pg_identifier(schema, "schema")
    es = _tbl(schema, "email_scrapes")
    seen: set[tuple[str, str]] = set()
    updates: list[tuple[str, str, Optional[str]]] = []
    skipped_no_job_or_date = 0

    for r in parsed_rows:
        jid = (r.get("job_post_id") or "").strip()
        date_iso = r.get("date")
        if not jid or not date_iso:
            skipped_no_job_or_date += 1
            continue
        key = (jid, _normalize_email_date_key(str(date_iso)))
        if key in seen:
            continue
        seen.add(key)
        loc = (r.get("location") or "").strip() or None
        updates.append((jid, str(date_iso).strip(), loc))

    rows_updated = 0
    would_change = 0
    matched_total = 0

    with conn.cursor() as cur:
        for jid, date_iso, loc in updates:
            if dry_run:
                cur.execute(
                    pg_sql.SQL(
                        """
                        SELECT COUNT(*) FROM {}
                        WHERE job_post_id = %s AND date IS NOT NULL
                          AND date_trunc('second', date) = date_trunc('second', %s::timestamptz);
                        """
                    ).format(es),
                    (jid, date_iso),
                )
                matched_total += cur.fetchone()[0]
                cur.execute(
                    pg_sql.SQL(
                        """
                        SELECT COUNT(*) FROM {}
                        WHERE job_post_id = %s AND date IS NOT NULL
                          AND date_trunc('second', date) = date_trunc('second', %s::timestamptz)
                          AND location IS DISTINCT FROM %s;
                        """
                    ).format(es),
                    (jid, date_iso, loc),
                )
                would_change += cur.fetchone()[0]
            else:
                cur.execute(
                    pg_sql.SQL(
                        """
                        UPDATE {}
                        SET location = %s
                        WHERE job_post_id = %s AND date IS NOT NULL
                          AND date_trunc('second', date) = date_trunc('second', %s::timestamptz)
                          AND location IS DISTINCT FROM %s;
                        """
                    ).format(es),
                    (loc, jid, date_iso, loc),
                )
                rows_updated += cur.rowcount

    out = {
        "candidates": len(updates),
        "skipped_no_job_or_date": skipped_no_job_or_date,
        "dry_run": dry_run,
    }
    if dry_run:
        out["email_scrape_rows_matched"] = matched_total
        out["rows_would_change"] = would_change
    else:
        out["rows_updated"] = rows_updated
    return out


def log_email_scrapes(
    conn, run_id: int, rows: list[dict], columns: list[str], *, schema: str = "public"
) -> list[int]:
    """Insert email scrape rows for a run. Returns list of email_scrape ids."""
    if conn is None or run_id is None or not rows:
        return []
    schema = _validate_pg_identifier(schema, "schema")
    es = _tbl(schema, "email_scrapes")
    ids = []
    with conn.cursor() as cur:
        for r in rows:
            cur.execute(
                pg_sql.SQL(
                    """
                    INSERT INTO {} (run_id, job_post_id, location, action_or_change, view_job_link, subject, date, from_)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id;
                    """
                ).format(es),
                (
                    run_id,
                    (r.get("job_post_id") or "").strip() or None,
                    (r.get("location") or "").strip() or None,
                    (r.get("action_or_change") or "").strip() or None,
                    (r.get("view_job_link") or "").strip() or None,
                    (r.get("subject") or "").strip() or None,
                    r.get("date"),  # ISO string or None
                    (r.get("from_") or "").strip() or None,
                ),
            )
            row = cur.fetchone()
            if row:
                ids.append(row[0])
    conn.commit()
    return ids


def find_email_scrape_id(
    conn, job_post_id: str, date_iso: Optional[str], *, schema: str = "public"
) -> Optional[int]:
    """Find email_scrapes.id by job_post_id and exact email date (full ISO) for 1:1 mapping."""
    if conn is None or not job_post_id or not HAS_PSYCOPG2 or pg_sql is None:
        return None
    schema = _validate_pg_identifier(schema, "schema")
    es = _tbl(schema, "email_scrapes")
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        if date_iso and date_iso.strip():
            try:
                cur.execute(
                    pg_sql.SQL(
                        """
                        SELECT id FROM {}
                        WHERE job_post_id = %s AND date_trunc('second', date) = date_trunc('second', %s::timestamptz)
                        ORDER BY id DESC LIMIT 1;
                        """
                    ).format(es),
                    (job_post_id.strip(), date_iso.strip()),
                )
            except Exception:
                cur.execute(
                    pg_sql.SQL(
                        """
                        SELECT id FROM {}
                        WHERE job_post_id = %s AND date::date = %s::date
                        ORDER BY id DESC LIMIT 1;
                        """
                    ).format(es),
                    (job_post_id.strip(), date_iso[:10] if len(date_iso) >= 10 else date_iso),
                )
        else:
            cur.execute(
                pg_sql.SQL(
                    "SELECT id FROM {} WHERE job_post_id = %s ORDER BY id DESC LIMIT 1;"
                ).format(es),
                (job_post_id.strip(),),
            )
        row = cur.fetchone()
        return row["id"] if row else None


def fetch_email_scrapes_with_job_links(
    conn,
    *,
    schema: str = "public",
    limit: Optional[int] = None,
    only_without_job_content: bool = False,
    only_sparse_job_content: bool = False,
    min_description_length: int = 80,
) -> list[dict]:
    """
    Rows from email_scrapes with a non-empty view_job_link (for Playwright link batch).

    Returns dicts with keys email_scrape_id, job_post_id, location, action_or_change,
    view_job_link, subject, date (ISO string), from_ — Gmail CSV shape plus id for upserts.
    """
    if conn is None or not HAS_PSYCOPG2 or pg_sql is None:
        return []
    schema = _validate_pg_identifier(schema, "schema")
    es = _tbl(schema, "email_scrapes")
    params: list = []
    min_len = max(0, int(min_description_length))
    if only_without_job_content and only_sparse_job_content:
        raise ValueError("only_without_job_content and only_sparse_job_content are mutually exclusive.")
    if only_without_job_content:
        jc = _tbl(schema, "job_content")
        q = pg_sql.SQL(
            """
            SELECT e.id AS email_scrape_id, e.job_post_id, e.location, e.action_or_change,
                   e.view_job_link, e.subject, e.date, e.from_
            FROM {} e
            LEFT JOIN {} j ON j.email_scrape_id = e.id
            WHERE e.view_job_link IS NOT NULL AND btrim(e.view_job_link) <> ''
              AND j.id IS NULL
            ORDER BY e.id DESC
            """
        ).format(es, jc)
    elif only_sparse_job_content:
        jc = _tbl(schema, "job_content")
        q = pg_sql.SQL(
            """
            SELECT e.id AS email_scrape_id, e.job_post_id, e.location, e.action_or_change,
                   e.view_job_link, e.subject, e.date, e.from_
            FROM {} e
            INNER JOIN {} j ON j.email_scrape_id = e.id
            WHERE e.view_job_link IS NOT NULL AND btrim(e.view_job_link) <> ''
              AND (
                j.description_full_text IS NULL
                OR length(btrim(coalesce(j.description_full_text, ''))) < %s
              )
            ORDER BY e.id DESC
            """
        ).format(es, jc)
        params.append(min_len)
    else:
        q = pg_sql.SQL(
            """
            SELECT id AS email_scrape_id, job_post_id, location, action_or_change,
                   view_job_link, subject, date, from_
            FROM {}
            WHERE view_job_link IS NOT NULL AND btrim(view_job_link) <> ''
            ORDER BY id DESC
            """
        ).format(es)
    if limit is not None:
        lim = max(1, int(limit))
        q = pg_sql.Composed([q, pg_sql.SQL(" LIMIT %s")])
        params.append(lim)
    rows_out: list[dict] = []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(q, params)
        for row in cur.fetchall():
            r = dict(row)
            esid = r.get("email_scrape_id")
            if esid is not None:
                try:
                    r["email_scrape_id"] = int(esid)
                except (TypeError, ValueError):
                    pass
            d = r.get("date")
            if d is not None and hasattr(d, "isoformat"):
                r["date"] = d.isoformat()
            elif d is not None:
                r["date"] = str(d)
            else:
                r["date"] = ""
            rows_out.append(r)
    return rows_out


def fetch_view_job_link_for_email_scrape(
    conn, email_scrape_id: int, *, schema: str = "public"
) -> Optional[str]:
    """Return view_job_link from email_scrapes for this id."""
    if conn is None or not email_scrape_id or not HAS_PSYCOPG2 or pg_sql is None:
        return None
    schema = _validate_pg_identifier(schema, "schema")
    es = _tbl(schema, "email_scrapes")
    with conn.cursor() as cur:
        cur.execute(
            pg_sql.SQL("SELECT view_job_link FROM {} WHERE id = %s LIMIT 1;").format(es),
            (int(email_scrape_id),),
        )
        row = cur.fetchone()
        if not row or row[0] is None:
            return None
        s = str(row[0]).strip()
        return s if s else None


def _txt(cleaned: dict, key: str) -> Optional[str]:
    v = cleaned.get(key)
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _types_of_cases(cleaned: dict) -> Optional[str]:
    """Persisted column: required_procedures and additional_requirements joined with newline."""
    rp = _txt(cleaned, "required_procedures")
    ar = _txt(cleaned, "additional_requirements")
    if rp and ar:
        return f"{rp}\n{ar}"
    return rp or ar


# Columns compared for ``scrape_change_summary`` (human-readable “what changed” vs email subject).
_JOB_CONTENT_SUMMARY_COLUMNS: tuple[str, ...] = (
    "job_post_id",
    "email_received_date",
    "view_job_link",
    "job_id",
    "title_line",
    "location_line",
    "practice_value",
    "city",
    "state",
    "address_line",
    "job_title",
    "posting_org",
    "priority",
    "status",
    "point_of_contact",
    "provider_start_date",
    "provider_end_date",
    "posted_date",
    "insight",
    "dates_needed",
    "standard_schedule",
    "types_of_cases",
    "support_staff",
    "avg_patients_per_day",
    "roster_only",
    "sf_primary_account_id",
    "sf_worksite_account_id",
    "sf_worksite_display_label",
    "sf_job_id",
    "description_full_text",
)

_SUMMARY_LABELS: dict[str, str] = {
    "job_post_id": "job post ID",
    "email_received_date": "email date",
    "view_job_link": "job link",
    "job_id": "Kimedics job ID",
    "title_line": "title line",
    "location_line": "location line",
    "practice_value": "practice",
    "city": "city",
    "state": "state",
    "address_line": "address",
    "job_title": "job title",
    "posting_org": "posting org",
    "priority": "priority",
    "status": "status",
    "point_of_contact": "point of contact",
    "provider_start_date": "provider start date",
    "provider_end_date": "provider end date",
    "posted_date": "posted date",
    "insight": "insight",
    "dates_needed": "dates needed",
    "standard_schedule": "standard schedule",
    "types_of_cases": "types of cases",
    "support_staff": "support staff",
    "avg_patients_per_day": "avg patients per day",
    "roster_only": "roster only",
    "sf_primary_account_id": "SF primary account",
    "sf_worksite_account_id": "SF worksite account",
    "sf_worksite_display_label": "SF worksite label",
    "sf_job_id": "SF job Id",
    "description_full_text": "job description",
}


def _norm_for_scrape_summary(val) -> str:
    if val is None:
        return ""
    if hasattr(val, "isoformat"):
        try:
            s = val.isoformat()
            return str(s)[:10] if len(str(s)) >= 10 else str(s).strip()
        except Exception:
            pass
    return str(val).strip()


def _fetch_latest_prior_job_content_summary(
    conn,
    *,
    canonical_job_key: str,
    schema: str,
) -> Optional[dict[str, Any]]:
    """
    Most recent ``job_content`` row for the same Kimedics job (by ``job_id`` or ``job_post_id``),
    for summarizing a **new** ``job_content`` insert (new email) vs prior notifications.
    """
    if (
        not canonical_job_key
        or conn is None
        or not HAS_PSYCOPG2
        or pg_sql is None
    ):
        return None
    schema = _validate_pg_identifier(schema, "schema")
    jt = _tbl(schema, "job_content")
    idents = [pg_sql.Identifier(c) for c in _JOB_CONTENT_SUMMARY_COLUMNS]
    _sel_cols = pg_sql.SQL(", ").join(idents)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            pg_sql.SQL(
                """
                SELECT {cols} FROM {jt}
                WHERE job_id = %s OR job_post_id = %s
                ORDER BY id DESC
                LIMIT 1;
                """
            ).format(cols=_sel_cols, jt=jt),
            (canonical_job_key, canonical_job_key),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {k: row.get(k) for k in _JOB_CONTENT_SUMMARY_COLUMNS}


def _job_content_snapshot_for_summary(
    *,
    job_post_id: str,
    date_only,
    resolved_link: str,
    cleaned: dict,
) -> dict[str, Any]:
    """Shape aligned with ``_JOB_CONTENT_SUMMARY_COLUMNS`` for diffing."""
    c = cleaned
    return {
        "job_post_id": (job_post_id or "").strip(),
        "email_received_date": date_only,
        "view_job_link": (resolved_link or "").strip() or None,
        "job_id": _txt(c, "job_id") if c.get("job_id") is not None else None,
        "title_line": c.get("title_line"),
        "location_line": c.get("location_line"),
        "practice_value": c.get("practice_value"),
        "city": _txt(c, "city"),
        "state": _txt(c, "state"),
        "address_line": _txt(c, "address_line"),
        "job_title": c.get("job_title"),
        "posting_org": c.get("posting_org"),
        "priority": c.get("priority"),
        "status": c.get("status"),
        "point_of_contact": c.get("point_of_contact"),
        "provider_start_date": c.get("provider_start_date"),
        "provider_end_date": c.get("provider_end_date"),
        "posted_date": _txt(c, "posted_date"),
        "insight": _txt(c, "insight"),
        "dates_needed": _txt(c, "dates_needed"),
        "standard_schedule": _txt(c, "standard_schedule"),
        "types_of_cases": _types_of_cases(c),
        "support_staff": _txt(c, "support_staff"),
        "avg_patients_per_day": _txt(c, "avg_patients_per_day"),
        "roster_only": _txt(c, "roster_only"),
        "sf_primary_account_id": _txt(c, "sf_primary_account_id"),
        "sf_worksite_account_id": _txt(c, "sf_worksite_account_id"),
        "sf_worksite_display_label": _txt(c, "sf_worksite_display_label"),
        "sf_job_id": _txt(c, "sf_job_id"),
        "description_full_text": c.get("description_full_text"),
    }


def _scrape_summary_changed_fields_truncated(
    changed: list[str],
    *,
    max_labels: int = 5,
) -> str:
    """Comma-separated field labels, capped so summaries stay short in the UI."""
    if not changed:
        return ""
    if len(changed) <= max_labels:
        return ", ".join(changed)
    head = ", ".join(changed[:max_labels])
    return f"{head} (+{len(changed) - max_labels} more)"


def _build_scrape_change_summary(
    *,
    updating_existing_row: bool,
    prev: Optional[dict],
    new_row: dict[str, Any],
    prior_same_job_snapshot: Optional[dict] = None,
) -> str:
    if not updating_existing_row:
        if prior_same_job_snapshot is None:
            return "First notify."
        changed_ns: list[str] = []
        for col in _JOB_CONTENT_SUMMARY_COLUMNS:
            if _norm_for_scrape_summary(prior_same_job_snapshot.get(col)) != _norm_for_scrape_summary(
                new_row.get(col)
            ):
                changed_ns.append(_SUMMARY_LABELS.get(col, col.replace("_", " ")))
        if not changed_ns:
            return "Later notify; no diffs."
        return "Later notify: " + _scrape_summary_changed_fields_truncated(changed_ns)
    if not prev:
        return "Rescrape; no baseline."
    changed: list[str] = []
    for col in _JOB_CONTENT_SUMMARY_COLUMNS:
        if _norm_for_scrape_summary(prev.get(col)) != _norm_for_scrape_summary(new_row.get(col)):
            changed.append(_SUMMARY_LABELS.get(col, col.replace("_", " ")))
    if not changed:
        return "Rescrape; no diffs."
    return "Rescrape: " + _scrape_summary_changed_fields_truncated(changed)


def log_job_content(
    conn,
    run_id: int,
    job_post_id: str,
    email_received_date: Optional[str],
    cleaned: dict,
    email_scrape_id: Optional[int] = None,
    *,
    schema: str = "public",
    sf_lookup_cache: Optional[dict] = None,
    view_job_link: Optional[str] = None,
) -> Optional[int]:
    """Insert or update one job_content row. One row per email_scrape_id (1:1 with email_scrapes)."""
    if conn is None or run_id is None or not HAS_PSYCOPG2 or pg_sql is None:
        return None
    schema = _validate_pg_identifier(schema, "schema")
    jt = _tbl(schema, "job_content")
    cleaned = dict(cleaned or {})
    try:
        from utils.job_sf_enrichment import enrich_cleaned_row_salesforce_fields

        enrich_cleaned_row_salesforce_fields(
            conn, cleaned, schema=schema, cache=sf_lookup_cache, run_id=run_id
        )
    except Exception:
        pass
    if email_scrape_id is None and job_post_id:
        email_scrape_id = find_email_scrape_id(conn, job_post_id, email_received_date, schema=schema)
    date_only = None
    if email_received_date:
        try:
            date_only = email_received_date[:10]
        except Exception:
            pass
    resolved_link = (view_job_link or "").strip() if view_job_link else ""
    if not resolved_link and email_scrape_id:
        resolved_link = fetch_view_job_link_for_email_scrape(
            conn, email_scrape_id, schema=schema
        ) or ""
    raw_json = json.dumps(cleaned) if cleaned else None
    vals = (
        run_id,
        email_scrape_id,
        (job_post_id or "").strip(),
        date_only,
        resolved_link or None,
        cleaned.get("job_id"),
        cleaned.get("title_line"),
        cleaned.get("location_line"),
        cleaned.get("practice_value"),
        _txt(cleaned, "city"),
        _txt(cleaned, "state"),
        _txt(cleaned, "address_line"),
        cleaned.get("job_title"),
        cleaned.get("posting_org"),
        cleaned.get("priority"),
        cleaned.get("status"),
        cleaned.get("point_of_contact"),
        cleaned.get("provider_start_date"),
        cleaned.get("provider_end_date"),
        _txt(cleaned, "posted_date"),
        _txt(cleaned, "insight"),
        _txt(cleaned, "dates_needed"),
        _txt(cleaned, "standard_schedule"),
        _types_of_cases(cleaned),
        _txt(cleaned, "support_staff"),
        _txt(cleaned, "avg_patients_per_day"),
        _txt(cleaned, "roster_only"),
        _txt(cleaned, "sf_primary_account_id"),
        _txt(cleaned, "sf_worksite_account_id"),
        _txt(cleaned, "sf_worksite_display_label"),
        _txt(cleaned, "sf_job_id"),
        cleaned.get("description_full_text"),
        raw_json,
    )
    row_snap = _job_content_snapshot_for_summary(
        job_post_id=(job_post_id or "").strip(),
        date_only=date_only,
        resolved_link=resolved_link,
        cleaned=cleaned,
    )
    content_id = None
    _sel_cols = pg_sql.SQL(", ").join(
        [pg_sql.Identifier("id")]
        + [pg_sql.Identifier(c) for c in _JOB_CONTENT_SUMMARY_COLUMNS]
    )
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        if email_scrape_id is not None:
            cur.execute(
                pg_sql.SQL("SELECT {} FROM {} WHERE email_scrape_id = %s;").format(_sel_cols, jt),
                (email_scrape_id,),
            )
            existing = cur.fetchone()
            if existing:
                prev = {k: existing.get(k) for k in _JOB_CONTENT_SUMMARY_COLUMNS}
                summary = _build_scrape_change_summary(
                    updating_existing_row=True,
                    prev=prev,
                    new_row=row_snap,
                )
                cur.execute(
                    pg_sql.SQL(
                        """
                        UPDATE {} SET
                            run_id = %s, job_post_id = %s, email_received_date = %s, view_job_link = %s,
                            job_id = %s, title_line = %s, location_line = %s, practice_value = %s,
                            city = %s, state = %s, address_line = %s,
                            job_title = %s, posting_org = %s, priority = %s, status = %s,
                            point_of_contact = %s, provider_start_date = %s, provider_end_date = %s,
                            posted_date = %s,
                            insight = %s, dates_needed = %s, standard_schedule = %s,
                            types_of_cases = %s, support_staff = %s, avg_patients_per_day = %s, roster_only = %s,
                            sf_primary_account_id = %s, sf_worksite_account_id = %s, sf_worksite_display_label = %s,
                            sf_job_id = %s,
                            description_full_text = %s, raw_columns_json = %s,
                            scrape_change_summary = %s
                        WHERE email_scrape_id = %s
                        RETURNING id;
                        """
                    ).format(jt),
                    (vals[0], *vals[2:], summary, email_scrape_id),
                )
                row = cur.fetchone()
                content_id = int(row["id"]) if row and row.get("id") is not None else None
                if content_id is not None:
                    _upsert_job_current(
                        conn,
                        content_id,
                        (job_post_id or "").strip(),
                        cleaned,
                        schema=schema,
                        view_job_link=resolved_link or None,
                        run_id=run_id,
                    )
                return content_id
        canon = (str(cleaned.get("job_id") or "").strip() or str(job_post_id or "").strip())
        prior_same = _fetch_latest_prior_job_content_summary(
            conn, canonical_job_key=canon, schema=schema
        )
        summary_ins = _build_scrape_change_summary(
            updating_existing_row=False,
            prev=None,
            new_row=row_snap,
            prior_same_job_snapshot=prior_same,
        )
        cur.execute(
            pg_sql.SQL(
                """
                INSERT INTO {} (
                    run_id, email_scrape_id, job_post_id, email_received_date, view_job_link,
                    job_id, title_line, location_line, practice_value, city, state, address_line,
                    job_title, posting_org, priority, status, point_of_contact,
                    provider_start_date, provider_end_date, posted_date,
                    insight, dates_needed, standard_schedule, types_of_cases,
                    support_staff, avg_patients_per_day, roster_only,
                    sf_primary_account_id, sf_worksite_account_id, sf_worksite_display_label,
                    sf_job_id,
                    description_full_text, raw_columns_json, scrape_change_summary
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                RETURNING id;
                """
            ).format(jt),
            (*vals, summary_ins),
        )
        row = cur.fetchone()
        content_id = int(row["id"]) if row and row.get("id") is not None else None
    if content_id is not None:
        _upsert_job_current(
            conn,
            content_id,
            (job_post_id or "").strip(),
            cleaned,
            schema=schema,
            view_job_link=resolved_link or None,
            run_id=run_id,
        )
    return content_id


def _upsert_job_current(
    conn,
    job_content_id: int,
    job_post_id: str,
    cleaned: dict,
    *,
    schema: str = "public",
    view_job_link: Optional[str] = None,
    run_id: Optional[int] = None,
) -> None:
    """Keep job_current as the latest state per job_id. Called after each job_content insert/update."""
    if conn is None or not HAS_PSYCOPG2 or pg_sql is None:
        return
    schema = _validate_pg_identifier(schema, "schema")
    jcur = _tbl(schema, "job_current")
    job_id = (cleaned.get("job_id") or job_post_id or "").strip()
    if not job_id:
        return
    with conn.cursor() as cur:
        # Capture SF ids before insert/update so we can log accurate prev/next.
        cur.execute(
            pg_sql.SQL("SELECT 1 FROM {} WHERE job_id = %s LIMIT 1;").format(jcur),
            (job_id,),
        )
        had_existing_job_current = cur.fetchone() is not None

        cur.execute(
            pg_sql.SQL(
                "SELECT sf_worksite_account_id, sf_job_id FROM {} WHERE job_id = %s LIMIT 1;"
            ).format(jcur),
            (job_id,),
        )
        prev_row = cur.fetchone() or (None, None)
        prev_w = (prev_row[0] or "").strip() if prev_row[0] is not None else ""
        prev_j = (prev_row[1] or "").strip() if prev_row[1] is not None else ""

        cur.execute(
            pg_sql.SQL(
                """
                INSERT INTO {} (
                    job_id, title_line, location_line, practice_value, city, state, address_line,
                    job_title, posting_org, priority, status, point_of_contact,
                    provider_start_date, provider_end_date, posted_date, view_job_link,
                    insight, dates_needed, standard_schedule, types_of_cases,
                    support_staff, avg_patients_per_day, roster_only,
                    sf_primary_account_id, sf_worksite_account_id, sf_worksite_display_label,
                    sf_job_id,
                    description_full_text, last_job_content_id, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW()
                )
                ON CONFLICT (job_id) DO UPDATE SET
                    title_line = EXCLUDED.title_line,
                    location_line = EXCLUDED.location_line,
                    practice_value = EXCLUDED.practice_value,
                    city = EXCLUDED.city,
                    state = EXCLUDED.state,
                    address_line = EXCLUDED.address_line,
                    job_title = EXCLUDED.job_title,
                    posting_org = EXCLUDED.posting_org,
                    priority = EXCLUDED.priority,
                    status = EXCLUDED.status,
                    point_of_contact = EXCLUDED.point_of_contact,
                    provider_start_date = EXCLUDED.provider_start_date,
                    provider_end_date = EXCLUDED.provider_end_date,
                    posted_date = EXCLUDED.posted_date,
                    view_job_link = EXCLUDED.view_job_link,
                    insight = EXCLUDED.insight,
                    dates_needed = EXCLUDED.dates_needed,
                    standard_schedule = EXCLUDED.standard_schedule,
                    types_of_cases = EXCLUDED.types_of_cases,
                    support_staff = EXCLUDED.support_staff,
                    avg_patients_per_day = EXCLUDED.avg_patients_per_day,
                    roster_only = EXCLUDED.roster_only,
                    sf_primary_account_id = EXCLUDED.sf_primary_account_id,
                    sf_worksite_account_id = EXCLUDED.sf_worksite_account_id,
                    sf_worksite_display_label = EXCLUDED.sf_worksite_display_label,
                    sf_job_id = EXCLUDED.sf_job_id,
                    description_full_text = EXCLUDED.description_full_text,
                    last_job_content_id = EXCLUDED.last_job_content_id,
                    updated_at = NOW();
                """
            ).format(jcur),
            (
                job_id,
                cleaned.get("title_line"),
                cleaned.get("location_line"),
                cleaned.get("practice_value"),
                _txt(cleaned, "city"),
                _txt(cleaned, "state"),
                _txt(cleaned, "address_line"),
                cleaned.get("job_title"),
                cleaned.get("posting_org"),
                cleaned.get("priority"),
                cleaned.get("status"),
                cleaned.get("point_of_contact"),
                cleaned.get("provider_start_date"),
                cleaned.get("provider_end_date"),
                _txt(cleaned, "posted_date"),
                (view_job_link or "").strip() or None,
                _txt(cleaned, "insight"),
                _txt(cleaned, "dates_needed"),
                _txt(cleaned, "standard_schedule"),
                _types_of_cases(cleaned),
                _txt(cleaned, "support_staff"),
                _txt(cleaned, "avg_patients_per_day"),
                _txt(cleaned, "roster_only"),
                _txt(cleaned, "sf_primary_account_id"),
                _txt(cleaned, "sf_worksite_account_id"),
                _txt(cleaned, "sf_worksite_display_label"),
                _txt(cleaned, "sf_job_id"),
                cleaned.get("description_full_text"),
                job_content_id,
            ),
        )

        # Read the resulting SF ids and log prev/next when they differ.
        cur.execute(
            pg_sql.SQL(
                "SELECT sf_worksite_account_id, sf_job_id FROM {} WHERE job_id = %s LIMIT 1;"
            ).format(jcur),
            (job_id,),
        )
        next_row = cur.fetchone() or (None, None)
        next_w = (next_row[0] or "").strip() if next_row[0] is not None else ""
        next_j = (next_row[1] or "").strip() if next_row[1] is not None else ""

        changed_fields: list[str] = []
        if (prev_w or "") != (next_w or ""):
            changed_fields.append("sf_worksite_account_id")
        if (prev_j or "") != (next_j or ""):
            changed_fields.append("sf_job_id")

        if changed_fields:
            log_job_event(
                conn,
                job_id=job_id,
                event_type="job_current_sf_ids_changed",
                run_id=run_id,
                schema=schema,
                payload={
                    "source": "job_current_table",
                    "saved_as": (
                        "updated_existing_latest_row"
                        if had_existing_job_current
                        else "inserted_new_latest_row"
                    ),
                    "fields_changed": changed_fields,
                    "prev": {"sf_worksite_account_id": prev_w or None, "sf_job_id": prev_j or None},
                    "next": {"sf_worksite_account_id": next_w or None, "sf_job_id": next_j or None},
                    "job_content_id": int(job_content_id),
                },
            )
    conn.commit()


def get_job_content(
    conn,
    *,
    limit: Optional[int] = 500,
    schema: str = "public",
    job_ids: Optional[list[str]] = None,
) -> list[dict]:
    """
    Read job_content rows (historical / per-email scrape). Optional filter by Kimedics job_id.
    """
    if conn is None:
        return []
    schema_q = _validate_pg_identifier(schema, "schema")
    tbl = f'"{schema_q}"."job_content"'
    cols = (
        "id, run_id, email_scrape_id, job_post_id, email_received_date, view_job_link, "
        "job_id, title_line, location_line, practice_value, city, state, address_line, "
        "job_title, posting_org, priority, status, "
        "point_of_contact, provider_start_date, provider_end_date, posted_date, "
        "insight, dates_needed, standard_schedule, types_of_cases, support_staff, "
        "avg_patients_per_day, roster_only, "
        "sf_primary_account_id, sf_worksite_account_id, sf_worksite_display_label, "
        "sf_job_id, "
        "description_full_text, raw_columns_json, scrape_change_summary, created_at"
    )
    cursor_factory = RealDictCursor if HAS_PSYCOPG2 else None
    with conn.cursor(cursor_factory=cursor_factory) as cur:
        if job_ids:
            ph = ",".join(["%s"] * len(job_ids))
            sql = f"SELECT {cols} FROM {tbl} WHERE job_id IN ({ph}) ORDER BY id DESC"
            params: list = list(job_ids)
            if limit is not None:
                sql += " LIMIT %s"
                params.append(limit)
            cur.execute(sql, tuple(params))
        else:
            sql = f"SELECT {cols} FROM {tbl} ORDER BY id DESC"
            if limit is not None:
                sql += " LIMIT %s"
                cur.execute(sql, (limit,))
            else:
                cur.execute(sql)
        rows = cur.fetchall()
        colnames = [d[0] for d in cur.description] if cur.description else []
    if cursor_factory is None and rows and colnames:
        return [dict(zip(colnames, r)) for r in rows]
    return [dict(r) for r in rows]


def get_job_current(
    conn,
    job_ids: Optional[list[str]] = None,
    *,
    limit: Optional[int] = 1000,
    schema: str = "public",
) -> list[dict]:
    """
    Read job_current rows from Supabase. Optional filter by job_id list.
    Returns list of dicts (one per row). Use for manual scripts that need to
    map Supabase data to Salesforce updates.
    """
    if conn is None:
        return []
    schema_q = _validate_pg_identifier(schema, "schema")
    tbl = f'"{schema_q}"."job_current"'
    cols = (
        "job_id, title_line, location_line, practice_value, city, state, address_line, "
        "job_title, posting_org, priority, status, point_of_contact, "
        "provider_start_date, provider_end_date, posted_date, view_job_link, "
        "insight, dates_needed, standard_schedule, types_of_cases, support_staff, "
        "avg_patients_per_day, roster_only, "
        "sf_primary_account_id, sf_worksite_account_id, sf_worksite_display_label, "
        "sf_job_id, "
        "description_full_text, last_job_content_id, updated_at"
    )
    cursor_factory = RealDictCursor if HAS_PSYCOPG2 else None
    with conn.cursor(cursor_factory=cursor_factory) as cur:
        if job_ids:
            ph = ",".join(["%s"] * len(job_ids))
            sql = f"SELECT {cols} FROM {tbl} WHERE job_id IN ({ph}) ORDER BY updated_at DESC"
            params: list = list(job_ids)
            if limit is not None:
                sql += " LIMIT %s"
                params.append(limit)
            cur.execute(sql, tuple(params))
        else:
            sql = f"SELECT {cols} FROM {tbl} ORDER BY updated_at DESC"
            if limit is not None:
                sql += " LIMIT %s"
                cur.execute(sql, (limit,))
            else:
                cur.execute(sql)
        rows = cur.fetchall()
        colnames = [d[0] for d in cur.description] if cur.description else []
    if cursor_factory is None and rows and colnames:
        return [dict(zip(colnames, r)) for r in rows]
    return [dict(r) for r in rows]


def fetch_job_current_for_salesforce_push(
    conn, job_id: str, *, schema: str = "public"
) -> Optional[dict]:
    """
    One ``job_current`` row by primary key ``job_id``, as a mutable dict, with
    ``enrich_cleaned_row_salesforce_fields`` applied (SF account / worksite lookups).
    """
    jid = (job_id or "").strip()
    if not jid:
        return None
    rows = get_job_current(conn, job_ids=[jid], limit=1, schema=schema)
    if not rows:
        return None
    row = dict(rows[0])
    try:
        from utils.job_sf_enrichment import enrich_cleaned_row_salesforce_fields

        enrich_cleaned_row_salesforce_fields(conn, row, schema=schema)
    except Exception:
        pass
    return row


def load_job_current_row_for_salesforce(job_id: str, *, schema: str = "public") -> dict:
    """
    Open a connection, load and enrich one ``job_current`` row for Salesforce mapping.

    Raises:
        RuntimeError: if the database connection fails.
        ValueError: if no row exists for ``job_id``.
    """
    with get_conn() as conn:
        if not conn:
            raise RuntimeError(
                "Could not connect to Postgres (install psycopg2, set DB_PASSWORD / DATABASE_URL)."
            )
        row = fetch_job_current_for_salesforce_push(conn, job_id, schema=schema)
    if not row:
        raise ValueError(
            f"No job_current row for job_id={job_id!r} in schema {schema!r}. "
            "Use a valid Kimedics job id from job_current or run the scrape pipeline first."
        )
    return row


def fetch_sf_reference_account_id(
    conn, reference_key: str, *, schema: str = "public"
) -> Optional[str]:
    """Return salesforce_account_id for a row in sf_account_reference (e.g. primary_aspen_dental_management)."""
    if conn is None or not HAS_PSYCOPG2 or pg_sql is None:
        return None
    schema = _validate_pg_identifier(schema, "schema")
    ref = _tbl(schema, "sf_account_reference")
    with conn.cursor() as cur:
        cur.execute(
            pg_sql.SQL(
                "SELECT salesforce_account_id FROM {} WHERE reference_key = %s LIMIT 1;"
            ).format(ref),
            (reference_key.strip(),),
        )
        row = cur.fetchone()
        return str(row[0]).strip() if row and row[0] else None


def update_sf_ids_for_job(
    conn,
    *,
    job_id: str,
    sf_job_id: Optional[str] = None,
    sf_worksite_account_id: Optional[str] = None,
    source: Optional[str] = None,
    mapping_status: Optional[str] = None,
    mapping_detail: Optional[str] = None,
    run_id: Optional[int] = None,
    schema: str = "public",
) -> tuple[int, int]:
    """
    Update ONLY Salesforce id columns for a given Kimedics job_id on both job_current and job_content.

    Returns (job_current_updated_rows, job_content_updated_rows).
    """
    if conn is None or not HAS_PSYCOPG2 or pg_sql is None:
        return (0, 0)
    jid = (job_id or "").strip()
    if not jid:
        return (0, 0)
    schema = _validate_pg_identifier(schema, "schema")
    jcur = _tbl(schema, "job_current")
    jc = _tbl(schema, "job_content")
    w = (sf_worksite_account_id or "").strip() or None
    sj = (sf_job_id or "").strip() or None
    with conn.cursor() as cur:
        cur.execute(
            pg_sql.SQL("SELECT sf_worksite_account_id, sf_job_id FROM {} WHERE job_id = %s LIMIT 1;").format(
                jcur
            ),
            (jid,),
        )
        prev_row = cur.fetchone() or (None, None)
        prev_w = (prev_row[0] or "").strip() if prev_row[0] is not None else ""
        prev_j = (prev_row[1] or "").strip() if prev_row[1] is not None else ""
        cur.execute(
            pg_sql.SQL(
                """
                UPDATE {} SET
                    sf_worksite_account_id = COALESCE(%s, sf_worksite_account_id),
                    sf_job_id = COALESCE(%s, sf_job_id)
                WHERE job_id = %s;
                """
            ).format(jcur),
            (w, sj, jid),
        )
        n_cur = cur.rowcount
        cur.execute(
            pg_sql.SQL(
                """
                UPDATE {} SET
                    sf_worksite_account_id = COALESCE(%s, sf_worksite_account_id),
                    sf_job_id = COALESCE(%s, sf_job_id)
                WHERE job_id = %s;
                """
            ).format(jc),
            (w, sj, jid),
        )
        n_content = cur.rowcount

    # Audit event in the same transaction as the UPDATEs (log_job_event does not commit).
    next_w = prev_w if w is None else (w or "")
    next_j = prev_j if sj is None else (sj or "")
    changed_fields: list[str] = []
    if (prev_w or "") != (next_w or ""):
        changed_fields.append("sf_worksite_account_id")
    if (prev_j or "") != (next_j or ""):
        changed_fields.append("sf_job_id")
    if changed_fields:
        log_job_event(
            conn,
            job_id=jid,
            event_type="sf_ids_update",
            run_id=run_id,
            schema=schema,
            payload={
                "source": (source or "").strip() or None,
                "mapping_status": (mapping_status or "").strip() or None,
                "mapping_detail": (mapping_detail or "").strip() or None,
                "fields_changed": changed_fields,
                "prev": {"sf_worksite_account_id": prev_w or None, "sf_job_id": prev_j or None},
                "next": {"sf_worksite_account_id": next_w or None, "sf_job_id": next_j or None},
                "updated_rows": {"job_current": int(n_cur or 0), "job_content": int(n_content or 0)},
            },
        )
    conn.commit()
    return (int(n_cur or 0), int(n_content or 0))
