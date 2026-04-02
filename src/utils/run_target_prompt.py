"""
Local-only: force an explicit STAGING vs PRODUCTION choice (all caps) before writing to Supabase.

- PRODUCTION -> PostgreSQL schema ``public`` (default for all programmatic defaults in code).
- STAGING    -> schema from ``SUPABASE_STAGING_SCHEMA`` env, or ``staging``.

Modal production does not import this module.
"""

from __future__ import annotations

import os

PRODUCTION_INPUT = "PRODUCTION"
STAGING_INPUT = "STAGING"


def staging_schema_name() -> str:
    return (os.environ.get("SUPABASE_STAGING_SCHEMA") or "staging").strip() or "staging"


def prompt_pg_schema(*, staging_only: bool = False) -> str:
    """
    Block until the user types STAGING or PRODUCTION (exactly, all caps).

    Returns the PostgreSQL schema name to use: ``public`` or the configured staging schema.

    If ``staging_only`` is True, only STAGING is accepted (for staging-only tools).
    """
    st = staging_schema_name()
    if staging_only:
        prompt = (
            f"This script writes only to the staging schema ({st!r}).\n"
            f"Type {STAGING_INPUT} (all caps) to continue:\n> "
        )
        while True:
            raw = input(prompt).strip()
            if raw == STAGING_INPUT:
                return st
            print(f"Invalid. Type exactly {STAGING_INPUT} (all caps).\n")

    prompt = (
        "Supabase target — type one of the following (all caps):\n"
        f"  {PRODUCTION_INPUT}  -> schema public (production data)\n"
        f"  {STAGING_INPUT}      -> schema {st!r} (test mirror)\n"
        "> "
    )
    while True:
        raw = input(prompt).strip()
        if raw == PRODUCTION_INPUT:
            return "public"
        if raw == STAGING_INPUT:
            return st
        print(f"Invalid. Type exactly {STAGING_INPUT} or {PRODUCTION_INPUT} (all caps).\n")


def resolve_pg_schema(
    pg_schema: str | None,
    *,
    production_ok: bool = False,
    staging_only: bool = False,
) -> str:
    """
    Use an explicit schema name from the CLI (no typing STAGING/PRODUCTION), or fall back to
    :func:`prompt_pg_schema`.

    - ``public`` is allowed only when ``production_ok`` is True.
    - When ``staging_only`` is True, ``public`` is never allowed (even if ``production_ok``).
    """
    if pg_schema is not None and (raw := pg_schema.strip()):
        from utils.supabase_db import _validate_pg_identifier

        s = _validate_pg_identifier(raw, "schema")
        if staging_only and s == "public":
            raise ValueError("This command does not target schema public.")
        if s == "public" and not production_ok:
            raise ValueError("Target schema public requires flag --production-ok.")
        return s
    return prompt_pg_schema(staging_only=staging_only)
