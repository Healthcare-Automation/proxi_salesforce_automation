"""
Salesforce push recovery engine.

Given a recent ``sf_scrape_fields_error`` row in ``job_event_log``, rebuild the
PATCH body from the latest Supabase state and retry the push. If the parser has
been updated since the failure, the retry usually succeeds. Otherwise the
offending fields are dropped from the body so the rest of the row lands, and a
``sf_field_quarantined`` event flags the fields for a human / parser fix.

Two entrypoints:
    * ``recover_recent_failures`` — scan and replay; used by the Modal job's
      auto-recovery tail and by the on-demand CLI.
    * ``recover_job_push``        — replay one specific error event; used by the
      hub's admin UI via the Modal web endpoint.

The engine writes new events with ``run_id = original_error.run_id`` so the
hub's validation page shows error + recovery on the same run.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from utils.sf_error_classify import (
    ClassifiedError,
    build_label_to_api_from_describe,
    classify_sf_error,
)
from utils.sf_recovery_rules import evaluate as evaluate_heuristics

Action = Literal[
    "re_parsed",
    "field_dropped",
    "transient_retried",
    "quarantined",
    "unhandled",
    "skipped",
    "dry_run",
]


@dataclass(frozen=True)
class RecoveryResult:
    job_id: str
    action: Action
    fields_pushed: list[str] = field(default_factory=list)
    fields_quarantined: list[str] = field(default_factory=list)
    error: Optional[str] = None
    original_event_id: Optional[int] = None
    original_run_id: Optional[int] = None
    sf_job_id: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


# ────────────────────────────────────────────────────────────────────────────
# Candidate selection
# ────────────────────────────────────────────────────────────────────────────

_CANDIDATE_SQL = """
    WITH latest_err AS (
        SELECT DISTINCT ON (jel.job_id)
            jel.id, jel.job_id, jel.event_type, jel.run_id, jel.payload, jel.created_at
        FROM {schema}.job_event_log jel
        WHERE jel.event_type IN (
                'sf_scrape_fields_error',
                'sf_mapping_pull_failed',
                'mapping_blocked_no_practice_value'
            )
          AND jel.created_at >= %(since)s
          {job_filter}
        ORDER BY jel.job_id, jel.created_at DESC
    )
    SELECT le.*
    FROM latest_err le
    WHERE NOT EXISTS (
        SELECT 1 FROM {schema}.job_event_log ok
        WHERE ok.job_id = le.job_id
          AND ok.event_type IN (
                'sf_scrape_fields_patched',
                'sf_scrape_fields_recovered',
                'job_created_in_salesforce',
                'sf_ids_update'
            )
          AND ok.created_at >= le.created_at
    )
    ORDER BY le.created_at ASC
    {limit}
"""


def _fetch_candidates(
    conn,
    *,
    since: datetime,
    schema: str,
    limit: Optional[int],
    job_ids: Optional[list[str]],
) -> list[dict]:
    """Return candidate error events to replay (most-recent-error-per-job, not yet resolved)."""
    from psycopg2.extras import RealDictCursor

    job_filter = ""
    params: dict[str, Any] = {"since": since}
    if job_ids:
        job_filter = "AND jel.job_id = ANY(%(job_ids)s)"
        params["job_ids"] = list(job_ids)
    lim = ""
    if limit:
        lim = "LIMIT %(limit)s"
        params["limit"] = int(limit)
    sql = _CANDIDATE_SQL.format(schema=schema, job_filter=job_filter, limit=lim)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


# ────────────────────────────────────────────────────────────────────────────
# parser_version (short git SHA of job_content_parser.py)
# ────────────────────────────────────────────────────────────────────────────

_parser_version_cache: Optional[str] = None


def _parser_version() -> str:
    global _parser_version_cache
    if _parser_version_cache is not None:
        return _parser_version_cache
    try:
        path = Path(__file__).resolve().parent / "job_content_parser.py"
        out = subprocess.run(
            ["git", "log", "-n", "1", "--pretty=%h", "--", str(path)],
            capture_output=True, text=True, timeout=5,
        )
        sha = out.stdout.strip()
        if sha:
            _parser_version_cache = sha
            return sha
    except Exception:
        pass
    # Fallback: content hash
    try:
        import hashlib
        path = Path(__file__).resolve().parent / "job_content_parser.py"
        _parser_version_cache = hashlib.sha256(path.read_bytes()).hexdigest()[:8]
        return _parser_version_cache
    except Exception:
        _parser_version_cache = "unknown"
        return _parser_version_cache


# ────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ────────────────────────────────────────────────────────────────────────────

def recover_recent_failures(
    conn,
    *,
    access_token: Optional[str] = None,
    instance_url: Optional[str] = None,
    since: Optional[datetime] = None,
    hours: Optional[float] = None,
    recovery_run_id: Optional[int] = None,
    schema: str = "public",
    dry_run: bool = False,
    limit: Optional[int] = None,
    job_ids: Optional[list[str]] = None,
    invocation: str = "modal_auto",
    invoker: str = "",
    circuit_break_at: int = 10,
) -> list[RecoveryResult]:
    """
    Scan ``job_event_log`` for unresolved SF push errors and replay each.

    Window: ``since`` (preferred) or ``hours`` ago. Defaults to 3 h.
    Events emitted use ``run_id = original_error.run_id`` so they appear on
    the run page where the error originally happened.
    """
    if conn is None:
        return []
    if since is None:
        h = hours if hours is not None else 3.0
        since = datetime.now(timezone.utc) - timedelta(hours=float(h))

    candidates = _fetch_candidates(
        conn, since=since, schema=schema, limit=limit, job_ids=job_ids,
    )
    if not candidates:
        return []

    results: list[RecoveryResult] = []
    scraped_count = 0
    for ev in candidates:
        res = recover_job_push(
            conn,
            access_token=access_token,
            instance_url=instance_url,
            error_event=ev,
            recovery_run_id=recovery_run_id,
            schema=schema,
            dry_run=dry_run,
            invocation=invocation,
            invoker=invoker,
        )
        results.append(res)
        if res.action == "re_parsed":
            scraped_count += 1
        if scraped_count > circuit_break_at:
            # Systemic parser regression: do not hammer SF further. Log and stop.
            try:
                from utils.supabase_db import log_job_event
                log_job_event(
                    conn,
                    job_id=str(ev["job_id"]),
                    event_type="sf_recovery_circuit_open",
                    run_id=ev.get("run_id"),
                    schema=schema,
                    payload={
                        "message": f"Circuit breaker tripped after {scraped_count} re-parse recoveries in one pass",
                        "invocation": invocation,
                    },
                )
            except Exception:
                pass
            break
    return results


def recover_job_push(
    conn,
    *,
    access_token: Optional[str],
    instance_url: Optional[str],
    error_event: dict,
    recovery_run_id: Optional[int] = None,
    schema: str = "public",
    dry_run: bool = False,
    invocation: str = "modal_auto",
    invoker: str = "",
) -> RecoveryResult:
    """
    Replay one error event.

    Returns a ``RecoveryResult`` describing what happened. Emits one of:
        * ``sf_scrape_fields_recovered`` (success)
        * ``sf_field_quarantined``       (per field dropped)
        * ``sf_push_unhandled_error``    (unknown error class)
    All three carry ``run_id = error_event.run_id``.
    """
    from utils.supabase_db import log_job_event
    job_id = str(error_event.get("job_id") or "").strip()
    original_event_id = error_event.get("id")
    original_run_id = error_event.get("run_id")
    event_type = str(error_event.get("event_type") or "").strip()
    payload = error_event.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    err_text = str(payload.get("error") or "")
    attempt = dict(payload.get("attempt") or {})

    res_stub = lambda action, **kw: RecoveryResult(
        job_id=job_id,
        action=action,
        original_event_id=original_event_id if isinstance(original_event_id, int) else None,
        original_run_id=original_run_id if isinstance(original_run_id, int) else None,
        **kw,
    )

    if not job_id:
        return res_stub("skipped", error="missing job_id on error event")

    # Load current scraped state for this job
    from utils.supabase_db import get_job_current
    rows = get_job_current(conn, job_ids=[job_id], limit=1, schema=schema)
    job_row = rows[0] if rows else {}

    # ── mapping_blocked_no_practice_value: re-attempt mapping if practice_value has been filled. ──
    # This event is emitted by sf_job_supabase_resolve when we refused to auto-create
    # a Job__c because practice_value was empty. The fix is upstream (parser); we just
    # keep re-running the resolver until practice_value lands, then let it match/create
    # normally. If it's still empty, we re-emit the same event so the next pass sees it.
    if event_type == "mapping_blocked_no_practice_value":
        practice_now = (job_row.get("practice_value") or "").strip() if job_row else ""
        if not practice_now:
            if not dry_run:
                try:
                    log_job_event(
                        conn,
                        job_id=job_id,
                        event_type="mapping_blocked_no_practice_value",
                        run_id=original_run_id,
                        schema=schema,
                        payload={
                            "reason": "empty_practice_value",
                            "detail": (
                                "Recovery re-checked job_current.practice_value — still empty. "
                                "Will retry next pass. Upstream parser fix needed."
                            ),
                            "recovered_from_event_id": original_event_id,
                            "recovery_run_id": recovery_run_id,
                            "invocation": invocation,
                            "automation_kind": "salesforce_job_create_blocked",
                        },
                    )
                except Exception:
                    pass
            return res_stub(
                "skipped",
                error="practice_value still empty; awaiting parser fix",
            )
        if dry_run:
            return res_stub(
                "dry_run",
                error=f"would re-resolve job (practice_value now: {practice_now[:80]!r})",
            )
        try:
            from utils.sf_job_supabase_resolve import resolve_sf_ids_for_job_ids
            resolve_sf_ids_for_job_ids(
                conn, [job_id], schema=schema, run_id=recovery_run_id,
            )
            return res_stub(
                "re_parsed",
                sf_job_id=(job_row.get("sf_job_id") or None),
                error=None,
            )
        except Exception as e:
            return res_stub("unhandled", error=f"resolver retry failed: {str(e)[:300]}")

    # Build describe/label lookup once (needed to resolve "Volume" → "Job_Volume__c")
    describe: dict = {}
    label_to_api: dict[str, str] = {}
    if instance_url and access_token:
        try:
            from utils.sf_job_rest_minimal import describe_sobject
            describe = describe_sobject(instance_url, access_token, "Job__c") or {}
            label_to_api = build_label_to_api_from_describe(describe)
        except Exception:
            describe = {}
            label_to_api = {}

    classified = classify_sf_error(err_text, label_to_api=label_to_api)

    # Unknown error class → log and skip.
    if classified.error_class == "unknown":
        try:
            log_job_event(
                conn,
                job_id=job_id,
                event_type="sf_push_unhandled_error",
                run_id=original_run_id,
                schema=schema,
                payload={
                    "error": err_text,
                    "recovered_from_event_id": original_event_id,
                    "attempt": attempt,
                    "invocation": invocation,
                    "recovery_run_id": recovery_run_id,
                },
            )
        except Exception:
            pass
        return res_stub("unhandled", error=f"unknown SF error class: {err_text[:200]}")

    sf_job_id = (job_row.get("sf_job_id") or "").strip() or None

    # Compute fresh "desired" payload from the current scrape state.
    desired_fresh: dict[str, Any] = {}
    if job_row and describe:
        try:
            from utils.sf_job_payload import (
                _canonical_description_use_html,
                prepare_payload_for_write,
            )
            desired_fresh = prepare_payload_for_write(
                job_row,
                describe,
                use_canonical_description=True,
                for_update=True,
                description_use_html=_canonical_description_use_html(),
                conn=conn,
                schema=schema,
            )
        except Exception as e:
            desired_fresh = {}
            classified_note = f"desired rebuild failed: {e}"
        else:
            classified_note = ""
    else:
        classified_note = "no describe or job_row; skipping re-parse"

    # Did the re-parse *change* any of the offending fields?
    offending = classified.offending_fields or ()
    can_retry_with_fresh = False
    if offending and desired_fresh:
        for f in offending:
            old = _normalize(attempt.get(f))
            new = _normalize(desired_fresh.get(f))
            if old != new:
                can_retry_with_fresh = True
                break
    elif classified.error_class == "transient" and attempt:
        can_retry_with_fresh = True  # same body; retry once

    if dry_run:
        return res_stub(
            "dry_run",
            sf_job_id=sf_job_id,
            error=f"class={classified.error_class} offending={list(offending)} "
                  f"can_retry_with_fresh={can_retry_with_fresh}",
        )

    # Try: push with fresh/attempt body minus anything we know is still bad.
    body = _merge_body_for_retry(attempt, desired_fresh, offending, can_retry_with_fresh)

    # Prepare & coerce against describe if we have it.
    if describe and body:
        try:
            from utils.sf_job_payload import coerce_picklists_to_valid
            from utils.sf_partial_update import prepare_patch_payload
            coerce_picklists_to_valid(describe, body)
            body = prepare_patch_payload(describe, body, coerce_picklists=True)
        except Exception:
            pass

    action: Action = "skipped"
    fields_pushed: list[str] = []
    fields_quarantined: list[str] = []
    push_error: Optional[str] = None
    patch_ok = False

    # Iterative salvage: push; if SF returns a new ``too_large`` / ``required_missing`` /
    # ``bad_picklist`` error naming a different field, drop THAT field too and retry.
    # Caps at 4 rounds so a pathological row can't loop forever.
    dropped: set[str] = set(offending) if not can_retry_with_fresh else set()
    attempts = 0
    MAX_ROUNDS = 4
    base_for_body = desired_fresh if (desired_fresh and can_retry_with_fresh) else (attempt or desired_fresh)

    while attempts < MAX_ROUNDS and sf_job_id and instance_url and access_token:
        attempts += 1
        body = {k: v for k, v in (base_for_body or {}).items() if k not in dropped}
        body = {k: v for k, v in body.items()
                if v is not None and (not isinstance(v, str) or v.strip())}
        if describe and body:
            try:
                from utils.sf_job_payload import coerce_picklists_to_valid
                from utils.sf_partial_update import prepare_patch_payload
                coerce_picklists_to_valid(describe, body)
                body = prepare_patch_payload(describe, body, coerce_picklists=True)
            except Exception:
                pass
        if not body:
            break
        try:
            from utils.sf_job_rest_minimal import update_job_record
            update_job_record(instance_url, access_token, "Job__c", sf_job_id, body)
            patch_ok = True
            fields_pushed = sorted(body.keys())
            fields_quarantined = sorted(dropped)
            if classified.error_class == "transient" and not dropped:
                action = "transient_retried"
            elif can_retry_with_fresh and not dropped:
                action = "re_parsed"
            else:
                action = "field_dropped"
            push_error = None
            break
        except Exception as e:
            push_error = str(e)[:1500]
            # Re-classify the new error. If it names a new offending field we haven't
            # dropped yet, add it and loop. Otherwise stop — further retries won't help.
            new_cls = classify_sf_error(push_error, label_to_api=label_to_api)
            new_fields = [f for f in (new_cls.offending_fields or ()) if f and f not in dropped]
            if new_cls.error_class in ("too_large", "required_missing", "bad_picklist") and new_fields:
                for f in new_fields:
                    dropped.add(f)
                continue
            break

    # Log outcome
    parser_ver = _parser_version()
    if patch_ok:
        try:
            log_job_event(
                conn,
                job_id=job_id,
                event_type="sf_scrape_fields_recovered",
                run_id=original_run_id,
                schema=schema,
                payload={
                    "recovered_from_event_id": original_event_id,
                    "original_error": err_text,
                    "original_run_id": original_run_id,
                    "recovery_run_id": recovery_run_id,
                    "invocation": invocation,
                    "invoker": invoker,
                    "action": action,
                    "offending_fields": list(offending),
                    "fields_pushed": fields_pushed,
                    "fields_quarantined": fields_quarantined,
                    "sf_job_id": sf_job_id,
                    "parser_version": parser_ver,
                },
            )
        except Exception:
            pass
        # Quarantine events (one per dropped field)
        for qf in fields_quarantined:
            _log_quarantine(
                conn,
                job_id=job_id,
                field=qf,
                bad_value=attempt.get(qf),
                attempt=attempt,
                err_text=err_text,
                max_length=classified.max_length,
                job_row=job_row,
                original_run_id=original_run_id,
                original_event_id=original_event_id,
                parser_version=parser_ver,
                invocation=invocation,
                schema=schema,
            )
        return RecoveryResult(
            job_id=job_id,
            action=action,
            fields_pushed=fields_pushed,
            fields_quarantined=fields_quarantined,
            original_event_id=original_event_id if isinstance(original_event_id, int) else None,
            original_run_id=original_run_id if isinstance(original_run_id, int) else None,
            sf_job_id=sf_job_id,
        )

    # Couldn't push. Quarantine offending fields for record-keeping and return.
    for qf in offending:
        _log_quarantine(
            conn,
            job_id=job_id,
            field=qf,
            bad_value=attempt.get(qf),
            attempt=attempt,
            err_text=err_text,
            max_length=classified.max_length,
            job_row=job_row,
            original_run_id=original_run_id,
            original_event_id=original_event_id,
            parser_version=parser_ver,
            invocation=invocation,
            schema=schema,
        )
    return res_stub(
        "quarantined",
        fields_quarantined=list(offending),
        sf_job_id=sf_job_id,
        error=push_error or classified_note or f"no push attempted (class={classified.error_class})",
    )


def _merge_body_for_retry(
    attempt: dict[str, Any],
    desired_fresh: dict[str, Any],
    offending: tuple[str, ...],
    can_retry_with_fresh: bool,
) -> dict[str, Any]:
    """
    Build the PATCH body for the retry:
        * Start from ``desired_fresh`` if available and it changed offending fields;
          otherwise from ``attempt``.
        * Drop any empty / obviously broken fields.
        * If we don't have a fresh fix for the offending field, leave it out (salvage).
    """
    base = dict(desired_fresh) if (desired_fresh and can_retry_with_fresh) else dict(attempt)
    out: dict[str, Any] = {}
    for k, v in base.items():
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        out[k] = v
    if not can_retry_with_fresh:
        for f in offending:
            out.pop(f, None)
    return out


def _log_quarantine(
    conn,
    *,
    job_id: str,
    field: str,
    bad_value: Any,
    attempt: dict,
    err_text: str,
    max_length: Optional[int],
    job_row: dict,
    original_run_id: Optional[int],
    original_event_id: Any,
    parser_version: str,
    invocation: str,
    schema: str,
) -> None:
    from utils.supabase_db import log_job_event
    siblings = {k: v for k, v in (attempt or {}).items() if k != field}
    heuristic = evaluate_heuristics(
        field=field,
        value=bad_value,
        siblings=siblings,
        max_length=max_length,
    ) or "n/a"
    try:
        log_job_event(
            conn,
            job_id=job_id,
            event_type="sf_field_quarantined",
            run_id=original_run_id,
            schema=schema,
            payload={
                "field": field,
                "bad_value": _truncate(bad_value, 500),
                "sibling_values": {k: _truncate(v, 200) for k, v in siblings.items()},
                "scrape_url": (job_row or {}).get("view_job_link"),
                "parser_version": parser_version,
                "heuristic_fired": heuristic,
                "recovered_from_event_id": original_event_id,
                "sf_error": _truncate(err_text, 500),
                "invocation": invocation,
            },
        )
    except Exception:
        pass


def _normalize(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v).strip()


def _truncate(v: Any, n: int) -> Any:
    if v is None:
        return None
    s = str(v)
    if len(s) <= n:
        return s
    return s[:n] + "…"


# ────────────────────────────────────────────────────────────────────────────
# Auth helper for scheduled / manual runs
# ────────────────────────────────────────────────────────────────────────────

def resolve_sf_credentials() -> Optional[tuple[str, str]]:
    """
    Resolve Salesforce credentials the same way ``sf_scrape_sync`` does.
    Returns (instance_url, access_token) or None if unavailable.
    """
    from utils.salesforce import get_token_auto
    ck = (os.environ.get("SALESFORCE_CONSUMER_KEY") or "").strip()
    cs = (os.environ.get("SALESFORCE_CONSUMER_SECRET") or "").strip()
    if not ck or not cs:
        return None
    use_cc = os.environ.get("SALESFORCE_USE_USERNAME_PASSWORD", "").lower() not in ("1", "true", "yes")
    try:
        tok = get_token_auto(
            ck,
            cs,
            os.environ.get("SALESFORCE_USERNAME") or None,
            os.environ.get("SALESFORCE_PASSWORD") or None,
            use_client_credentials=use_cc,
            token_url=os.environ.get("SALESFORCE_TOKEN_URL") or None,
            security_token=os.environ.get("SALESFORCE_SECURITY_TOKEN") or None,
            use_sandbox=os.environ.get("SALESFORCE_USE_SANDBOX", "").lower() in ("1", "true", "yes"),
        )
    except Exception:
        return None
    instance_url = (tok.get("instance_url") or "").strip() if isinstance(tok, dict) else ""
    access_token = (tok.get("access_token") or "").strip() if isinstance(tok, dict) else ""
    if not instance_url or not access_token:
        return None
    return instance_url, access_token
