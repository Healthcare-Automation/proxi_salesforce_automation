"""
Email alerting for the Proxi Salesforce Automation pipeline.

Sends from proxi@scrubnetwork.com via Gmail SMTP (uses GMAIL_APP_PASSWORD env var).
No extra dependencies — stdlib smtplib + email.mime only.

Two main entry points
---------------------
send_scrape_alert(job_post_id, issues, cleaned, view_job_link)
    Immediate alert when a scraped job fails validation.
    Called right after each scrape if should_send_immediate_alert() is True.

send_daily_summary(stats)
    Daily digest with counts, examples, and health metrics.
    Called by the Modal daily_summary scheduled function.
    If there were no emails, no job_content rows, and no pipeline runs in the
    period, returns False without sending (quiet day).
"""

from __future__ import annotations

import os
import smtplib
import textwrap
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Sequence, Optional

# ── Config ────────────────────────────────────────────────────────────────────

ALERT_RECIPIENTS = [
    "anddy0622@gmail.com",
    "seanhyang1@gmail.com",
    "proxi@scrubnetwork.com",
]
SMTP_HOST        = "smtp.gmail.com"
SMTP_PORT        = 587
_SENDER_DEFAULT  = "proxi@scrubnetwork.com"

# ── Base HTML template ────────────────────────────────────────────────────────

_BASE_STYLE = """
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         font-size:14px; color:#222; background:#f9f9f9; margin:0; padding:0; }
  .wrap { max-width:760px; margin:0 auto; background:#fff;
          border:1px solid #e0e0e0; border-radius:6px; overflow:hidden; }
  .header { background:#1a1a2e; color:#fff; padding:20px 28px; }
  .header h1 { margin:0; font-size:20px; font-weight:600; }
  .header p  { margin:4px 0 0; font-size:13px; opacity:.75; }
  .body { padding:24px 28px; }
  .section { margin-bottom:24px; }
  .section h2 { font-size:14px; font-weight:700; color:#444;
                text-transform:uppercase; letter-spacing:.5px;
                border-bottom:2px solid #f0f0f0; padding-bottom:6px; margin:0 0 12px; }
  table { border-collapse:collapse; width:100%; font-size:13px; }
  th { background:#f5f5f5; padding:7px 10px; text-align:left;
       font-size:12px; color:#666; font-weight:600; }
  td { padding:7px 10px; border-bottom:1px solid #f0f0f0; vertical-align:top; }
  tr:last-child td { border-bottom:none; }
  .badge { display:inline-block; padding:2px 8px; border-radius:12px;
           font-size:11px; font-weight:700; letter-spacing:.3px; }
  .badge-crit { background:#fde8e8; color:#c0392b; }
  .badge-warn { background:#fef3e2; color:#d35400; }
  .badge-info { background:#e8f4fb; color:#2980b9; }
  .badge-ok   { background:#e8f8f0; color:#27ae60; }
  .stat-row { display:flex; gap:16px; flex-wrap:wrap; margin-bottom:20px; }
  .stat-box { flex:1; min-width:120px; background:#f8f8f8; border:1px solid #e8e8e8;
              border-radius:6px; padding:14px 16px; text-align:center; }
  .stat-box .num { font-size:28px; font-weight:700; color:#1a1a2e; line-height:1; }
  .stat-box .lbl { font-size:11px; color:#888; margin-top:4px; text-transform:uppercase;
                   letter-spacing:.5px; }
  .footer { background:#f5f5f5; padding:14px 28px; font-size:11px; color:#999;
            border-top:1px solid #e8e8e8; }
  code { background:#f0f0f0; border-radius:3px; padding:1px 5px;
         font-family:monospace; font-size:12px; }
  .mono { font-family:monospace; font-size:12px; color:#555; }
</style>
"""


def _html_wrap(header_title: str, header_sub: str, body_html: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%b %d, %Y %H:%M UTC")
    return f"""<!DOCTYPE html><html><head>{_BASE_STYLE}</head><body>
<div class="wrap">
  <div class="header">
    <h1>{header_title}</h1>
    <p>{header_sub}</p>
  </div>
  <div class="body">{body_html}</div>
  <div class="footer">Proxi Salesforce Automation · {ts} · proxi@scrubnetwork.com</div>
</div>
</body></html>"""


# ── Low-level send ─────────────────────────────────────────────────────────────

def _send(
    subject: str,
    html:    str,
    text:    str = "",
    recipients: Optional[Sequence[str]] = None,
) -> bool:
    """Send an email. Returns True on success. Never raises."""
    sender      = os.environ.get("GMAIL_EMAIL", _SENDER_DEFAULT)
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not app_password:
        print("[alert_email] GMAIL_APP_PASSWORD not set — skipping email")
        return False
    to = list(recipients or ALERT_RECIPIENTS)
    if not to:
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"Proxi Automation <{sender}>"
        msg["To"]      = ", ".join(to)
        if text:
            msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as srv:
            srv.ehlo()
            srv.starttls()
            srv.login(sender, app_password)
            srv.sendmail(sender, to, msg.as_string())
        print(f"[alert_email] Sent '{subject}' → {to}")
        return True
    except Exception as exc:
        print(f"[alert_email] Failed to send email: {exc}")
        return False


# ── Immediate scrape alert ─────────────────────────────────────────────────────

def send_scrape_alert(
    job_post_id:   str,
    issues:        list,                 # list[ValidationIssue] from scrape_validator
    cleaned:       Optional[dict] = None,
    view_job_link: Optional[str] = None,
    *,
    sibling_success:  bool = False,
    downstream_events: Optional[list] = None,  # [(event_type, count), …]
    recent_history:   Optional[dict] = None,
    sf_mapping:       Optional[dict] = None,
    empty_row:        bool = False,
    batch_size:       int  = 1,
) -> bool:
    """
    Per-job consolidated alert email — fired from
    ``pipeline_link_scrape._decide_and_send_alerts`` after Pass 2 has gathered
    the full pipeline context (validator output + SF events + sibling-scrape
    outcome + recent history). Body now mirrors / exceeds the daily summary's
    error surface.

    Parameters
    ----------
    job_post_id      Kimedics job post id
    issues           list[ValidationIssue] from validate_scraped_job()
    cleaned          parsed job dict (snapshot section)
    view_job_link    direct link to the Kimedics page

    Keyword-only context (added so the alert matches daily-summary coverage):
    sibling_success    a sibling scrape for this job_id in the same batch
                       succeeded (alert is for an earlier transient blip)
    downstream_events  list of (event_type, count) for this run — e.g.
                       sf_scrape_fields_error, job_create_failed,
                       worksite_create_failed, sf_field_quarantined,
                       sf_sync_skipped_no_mapping
    recent_history     {unresolved_failed, auto_retries_24h, manual_rescr_24h,
                       failures_24h}
    sf_mapping         {sf_job_id, patched_fields, created_record}
    empty_row          this run wrote a job_content row with job_id NULL or
                       empty title — silent scrape failure (e.g. job 19782)
    batch_size         how many scrape rows in this batch for this job_id
    """
    from utils.scrape_validator import Severity, issues_as_html, issues_summary

    summary = issues_summary(issues)
    critical_n = summary["critical"]
    warning_n  = summary["warning"]
    cleaned    = cleaned or {}
    downstream_events = downstream_events or []
    recent_history    = recent_history or {}
    sf_mapping        = sf_mapping or {}

    # Total downstream-error count drives severity in subject.
    downstream_total = sum(n for _, n in downstream_events)
    unresolved       = int(recent_history.get("unresolved_failed", 0) or 0)

    # Subject — bake downstream signal in so operators can triage from inbox.
    parts: list[str] = []
    if critical_n > 0:
        parts.append(f"{critical_n} critical")
    if warning_n > 0 and critical_n == 0:
        parts.append(f"{warning_n} warning(s)")
    if downstream_total > 0:
        parts.append(f"{downstream_total} SF event(s)")
    if unresolved > 0:
        parts.append(f"{unresolved} unresolved")
    detail = " · ".join(parts) or "issues detected"
    if critical_n > 0 or unresolved > 0 or empty_row:
        subject = f"🚨 Scrape Alert — Job #{job_post_id} — {detail}"
    elif downstream_total > 0:
        subject = f"⚠️  Scrape + SF Alert — Job #{job_post_id} — {detail}"
    else:
        subject = f"⚠️  Scrape Warning — Job #{job_post_id} — {detail}"

    # Badge HTML
    def _badge(sev: str, n: int) -> str:
        cls = {"CRITICAL": "badge-crit", "WARNING": "badge-warn", "INFO": "badge-info"}.get(sev, "")
        return f'<span class="badge {cls}">{n} {sev}</span>&nbsp;' if n else ""

    badges = (
        _badge("CRITICAL", critical_n)
        + _badge("WARNING", warning_n)
        + _badge("INFO", summary["info"])
    )

    # Job link
    link_html = ""
    if view_job_link:
        link_html = (
            f'<p style="margin:0 0 16px;">'
            f'<a href="{view_job_link}" style="color:#2471a3;">'
            f'🔗 Open Job #{job_post_id} on Kimedics</a></p>'
        )

    # Data snapshot: key fields
    snapshot_fields = [
        ("title_line",          "Title"),
        ("job_title",           "Job Title"),
        ("status",              "Status"),
        ("rates",               "Rates"),
        ("provider_start_date", "Start Date"),
        ("provider_end_date",   "End Date"),
        ("location_line",       "Location"),
        ("state",               "State"),
        ("city",                "City"),
        ("posting_org",         "Posting Org"),
        ("point_of_contact",    "POC"),
        ("position_type",       "Position Type"),
        ("posted_date",         "Posted Date"),
    ]
    snap_rows = []
    for key, label in snapshot_fields:
        val = (cleaned.get(key) or "").strip()
        empty_style = ' style="color:#c0392b;font-style:italic;"' if not val else ""
        display = val if val else "— MISSING —"
        snap_rows.append(
            f'<tr><td style="color:#666;width:140px;">{label}</td>'
            f'<td{empty_style}>{display}</td></tr>'
        )
    desc_preview = (cleaned.get("description_full_text") or "")[:300]
    if desc_preview:
        snap_rows.append(
            f'<tr><td style="color:#666;vertical-align:top;">Description</td>'
            f'<td class="mono">{desc_preview}{"…" if len(cleaned.get("description_full_text",""))>300 else ""}</td></tr>'
        )

    # ── Sibling-batch banner ────────────────────────────────────────────────
    sibling_banner = ""
    if sibling_success:
        sibling_banner = """
        <div class="section" style="background:#ecfdf5;border:1px solid #a7f3d0;border-radius:6px;padding:12px 14px;">
          <p style="margin:0;color:#15803d;font-size:13px;">
            <strong>Note:</strong> A sibling scrape for the same job in the same batch <strong>succeeded</strong>.
            Salesforce was updated by that paired scrape — this alert reflects the transient first attempt only.
            If you only need to know whether SF stayed in sync, the answer is <strong>yes</strong>.
          </p>
        </div>"""

    # ── Empty-row banner (job_content with job_id NULL or empty title) ──────
    empty_banner = ""
    if empty_row:
        empty_banner = """
        <div class="section" style="background:#fef2f2;border:1px solid #fecaca;border-radius:6px;padding:12px 14px;">
          <p style="margin:0;color:#991b1b;font-size:13px;">
            <strong>Silent scrape failure:</strong> a <code>job_content</code> row was written this run with empty
            critical fields. Auto-retry will pick this up within ~5 min; if it persists, click Rescrape on the admin page.
          </p>
        </div>"""

    # ── Downstream Salesforce events (this run) ─────────────────────────────
    downstream_html = ""
    if downstream_events:
        ev_rows = []
        # Plain-language labels for non-technical readers.
        labels = {
            "sf_scrape_fields_error":            "SF field push error",
            "job_create_failed":                 "SF Job__c create failed",
            "worksite_create_failed":            "SF Worksite__c create failed",
            "sf_field_quarantined":              "Field quarantined (SF rejected)",
            "sf_sync_skipped_no_mapping":        "Sync skipped (no SF mapping)",
            "mapping_blocked_no_practice_value": "Blocked: empty practice_value (would create duplicate)",
            "scrape_silent_failure":             "Silent scrape failure (empty job_content row)",
        }
        for et, n in downstream_events:
            ev_rows.append(
                f'<tr><td style="color:#7f1d1d;font-weight:600;padding:4px 10px;font-size:12px;'
                f'white-space:nowrap;">{n}×</td>'
                f'<td style="padding:4px 10px;font-size:12px;">{labels.get(et, et)}</td>'
                f'<td style="padding:4px 10px;font-family:monospace;font-size:11px;color:#888;">{et}</td></tr>'
            )
        downstream_html = f"""
        <div class="section">
          <h2>Salesforce events on this run</h2>
          <table style="border-collapse:collapse;font-size:12px;">{''.join(ev_rows)}</table>
        </div>"""

    # ── SF mapping state ────────────────────────────────────────────────────
    sf_html = ""
    sfid = (sf_mapping.get("sf_job_id") or "").strip()
    patched_fields = int(sf_mapping.get("patched_fields", 0) or 0)
    created = bool(sf_mapping.get("created_record"))
    if sfid or patched_fields or created:
        sf_link = (
            f'<a href="https://proxi.lightning.force.com/lightning/r/Job__c/{sfid}/view" '
            f'style="color:#2471a3;">Open SF record · {sfid}</a>'
            if sfid else "—"
        )
        sf_html = f"""
        <div class="section">
          <h2>Salesforce state</h2>
          <table style="border-collapse:collapse;font-size:13px;">
            <tr><td style="color:#666;width:160px;padding:4px 10px;">sf_job_id</td><td style="padding:4px 10px;">{sf_link}</td></tr>
            <tr><td style="color:#666;padding:4px 10px;">Fields patched this run</td><td style="padding:4px 10px;">{patched_fields}</td></tr>
            <tr><td style="color:#666;padding:4px 10px;">New SF record created</td><td style="padding:4px 10px;">{'yes' if created else 'no'}</td></tr>
          </table>
        </div>"""

    # ── Recent history (last 24h) ───────────────────────────────────────────
    hist_html = ""
    if recent_history and any(recent_history.values()):
        un = recent_history.get("unresolved_failed", 0)
        au = recent_history.get("auto_retries_24h", 0)
        mn = recent_history.get("manual_rescr_24h", 0)
        fl = recent_history.get("failures_24h", 0)
        hist_html = f"""
        <div class="section">
          <h2>Recent activity (last 24 hours)</h2>
          <table style="border-collapse:collapse;font-size:13px;">
            <tr><td style="color:#666;width:200px;padding:4px 10px;">Unresolved failures</td>
                <td style="padding:4px 10px;color:{'#991b1b' if un else '#0f172a'};font-weight:{'700' if un else '400'};">{un}</td></tr>
            <tr><td style="color:#666;padding:4px 10px;">Auto-retries fired</td><td style="padding:4px 10px;">{au}</td></tr>
            <tr><td style="color:#666;padding:4px 10px;">Manual rescrapes</td><td style="padding:4px 10px;">{mn}</td></tr>
            <tr><td style="color:#666;padding:4px 10px;">Total failure events</td><td style="padding:4px 10px;">{fl}</td></tr>
          </table>
        </div>"""

    body = f"""
    <div class="section">
      <h2>Summary</h2>
      <p style="margin:0 0 8px;">Job <strong>#{job_post_id}</strong> flagged with: {badges}</p>
      {link_html}
    </div>

    {sibling_banner}
    {empty_banner}

    <div class="section">
      <h2>Validation Issues</h2>
      {issues_as_html(issues)}
    </div>

    {downstream_html}

    {sf_html}

    {hist_html}

    <div class="section">
      <h2>Scraped Data Snapshot</h2>
      <table>{''.join(snap_rows)}</table>
    </div>

    <div class="section">
      <p style="font-size:12px;color:#888;margin:0;">
        Consolidated per-job alert. The validator, this run's Salesforce events,
        sibling-batch outcomes, and the last 24 hours of activity were all
        consulted before sending. If a sibling scrape succeeded for this job in
        the same batch, Salesforce is already in sync — the banner at the top
        will say so.
      </p>
    </div>
    """

    # Plain-text fallback
    from utils.scrape_validator import issues_as_text
    text = textwrap.dedent(f"""
    Proxi Scrape Alert — Job #{job_post_id}
    {'='*50}
    Critical: {critical_n}  |  Warnings: {warning_n}

    Issues:
    {issues_as_text(issues, job_post_id)}

    Data snapshot:
    {chr(10).join(f'  {label}: {(cleaned.get(key) or "MISSING")}' for key, label in snapshot_fields)}

    Link: {view_job_link or 'N/A'}
    """).strip()

    return _send(
        subject,
        _html_wrap("🚨 Scrape Alert", f"Job #{job_post_id} · Proxi Automation", body),
        text,
    )


# ── Daily summary email ────────────────────────────────────────────────────────

def send_daily_summary(stats: dict) -> bool:
    """
    Send the daily digest email — one row per email_scrape received in the
    reporting window (yesterday 5:00 AM – 11:59 PM ET), with the downstream
    pipeline outcome summarized inline.

    Skips sending if no emails landed in the window. Expected stats keys:

      period_label         str
      emails_received      int
      scraped_ok           int   — job_content has title + description
      sf_mapped            int   — job_content has sf_job_id
      sf_jobs_created      int   — new Job__c created in SF during window
      worksites_created    int   — new Worksite__c (Account) created in SF during window
      field_patches_total  int   — total SF fields patched (across all emails)
      ext_id_swaps         int   — External_Job_ID__c repointed on existing record
      manual_rescrapes     int   — operator hit Rescrape in admin
      auto_retries         int   — cron auto-retried an orphaned scrape
      stuck_jobs           int   — unresolved job_create_failed (still needs help)
      scrape_failures      int   — scrape didn't produce real content + not stuck
      rows                 list[dict]  — one per email, see _build_daily_stats
    """
    g = stats.get

    period          = g("period_label",         "Last 24 hours")
    emails          = g("emails_received",      0)
    scraped_ok      = g("scraped_ok",           0)
    sf_mapped       = g("sf_mapped",            0)
    sf_jobs_created = g("sf_jobs_created",      0)
    worksites_created = g("worksites_created",  0)
    patches_total   = g("field_patches_total",  0)
    ext_id_swaps    = g("ext_id_swaps",         0)
    manual_rescr    = g("manual_rescrapes",     0)
    auto_retries    = g("auto_retries",         0)
    stuck_jobs      = g("stuck_jobs",           0)
    scrape_fails    = g("scrape_failures",      0)
    fields_quarantined    = g("fields_quarantined",      0)
    blocked_no_practice   = g("blocked_no_practice",     0)
    silent_failures       = g("silent_failures",         0)
    pushes_recovered      = g("pushes_recovered",        0)
    push_errors_total     = g("push_errors_total",       0)
    push_errors_unresolved = g("push_errors_unresolved", 0)
    rows            = g("rows",                 [])

    if emails == 0:
        print(
            "[alert_email] Daily summary skipped — no emails received in the "
            f"reporting period ({period})"
        )
        return False

    # ── Health badge ──────────────────────────────────────────────────────────
    if stuck_jobs > 0 or scrape_fails > 0 or push_errors_unresolved > 0 or blocked_no_practice > 0 or silent_failures > 0:
        health_badge = '<span class="badge badge-crit">NEEDS ATTENTION</span>'
        subject_pfx  = "🚨"
    elif ext_id_swaps > 0 or fields_quarantined > 0 or pushes_recovered > 0:
        health_badge = '<span class="badge badge-warn">REVIEW AMENDMENTS</span>'
        subject_pfx  = "⚠️"
    else:
        health_badge = '<span class="badge badge-ok">HEALTHY</span>'
        subject_pfx  = "✅"

    # Subject includes amendments when present so operators can triage from inbox.
    subject_amend = ""
    if pushes_recovered or fields_quarantined or push_errors_unresolved or blocked_no_practice or silent_failures:
        bits = []
        if pushes_recovered:       bits.append(f"{pushes_recovered} recovered")
        if fields_quarantined:     bits.append(f"{fields_quarantined} field{'s' if fields_quarantined != 1 else ''} dropped")
        if push_errors_unresolved: bits.append(f"{push_errors_unresolved} push err")
        if blocked_no_practice:    bits.append(f"{blocked_no_practice} blocked (no practice)")
        if silent_failures:        bits.append(f"{silent_failures} silent fail")
        subject_amend = " · " + " · ".join(bits)
    subject = (
        f"{subject_pfx} Proxi Daily — {period} — "
        f"{emails} emails · {scraped_ok} scraped · {sf_mapped} mapped · {patches_total} field patches"
        f"{subject_amend}"
    )

    # ── Stat boxes ────────────────────────────────────────────────────────────
    def _box(num, label, color="#1a1a2e"):
        return (
            f'<div class="stat-box">'
            f'<div class="num" style="color:{color};">{num}</div>'
            f'<div class="lbl">{label}</div></div>'
        )

    color_ok = "#27ae60"
    color_amber = "#d35400"
    color_red = "#c0392b"
    stats_html = (
        '<div class="stat-row">'
        + _box(emails,          "Emails Received")
        + _box(scraped_ok,      "Scraped OK",       color_ok if scraped_ok == emails else color_amber)
        + _box(sf_mapped,       "SF Job__c Mapped", color_ok if sf_mapped == emails else color_amber)
        + _box(sf_jobs_created, "New SF Job Records", "#0284c7" if sf_jobs_created else "#aaa")
        + _box(worksites_created, "New SF Worksites", "#0369a1" if worksites_created else "#aaa")
        + _box(patches_total,   "SF Fields Patched")
        + _box(ext_id_swaps,    "ID Swaps",         color_amber if ext_id_swaps else "#aaa")
        + _box(pushes_recovered, "Push Recovered",  color_amber if pushes_recovered else "#aaa")
        + _box(fields_quarantined, "Fields Dropped", color_amber if fields_quarantined else "#aaa")
        + _box(blocked_no_practice, "Blocked: No Practice", color_red if blocked_no_practice else "#aaa")
        + _box(silent_failures, "Silent Scrape Fails", color_red if silent_failures else "#aaa")
        + _box(push_errors_unresolved, "Push Errors", color_red if push_errors_unresolved else "#aaa")
        + _box(auto_retries,    "Auto Retries",     "#0e7490" if auto_retries else "#aaa")
        + _box(manual_rescr,    "Manual Rescrapes", "#0369a1" if manual_rescr else "#aaa")
        + _box(stuck_jobs,      "Stuck (needs fix)", color_red if stuck_jobs else "#aaa")
        + _box(scrape_fails,    "Scrape Failures",  color_red if scrape_fails else "#aaa")
        + "</div>"
    )

    # ── One row per email ─────────────────────────────────────────────────────
    def _chip(text: str, color: str, title: str = "") -> str:
        bg = {
            "green":  "#dcfce7", "amber": "#fef3c7", "red": "#fee2e2",
            "blue":   "#dbeafe", "cyan":  "#cffafe", "violet": "#ede9fe",
            "slate":  "#e2e8f0",
        }.get(color, "#e2e8f0")
        fg = {
            "green":  "#166534", "amber": "#92400e", "red": "#991b1b",
            "blue":   "#1e40af", "cyan":  "#155e75", "violet": "#5b21b6",
            "slate":  "#334155",
        }.get(color, "#334155")
        t = f' title="{title}"' if title else ""
        return (
            f'<span{t} style="display:inline-block;padding:1px 6px;border-radius:8px;'
            f'background:{bg};color:{fg};font-size:11px;font-weight:600;margin:1px 2px 1px 0;">{text}</span>'
        )

    def _email_kind_chip(subject_text: str, action: str) -> str:
        s = (subject_text or "").lower()
        a = (action or "").lower()
        if "new job post" in s:
            return _chip("new", "violet", "New job post from Aspen Dental")
        if a.startswith("status:"):
            tail = action.split(":", 1)[1].strip().lower()
            return _chip(f"status: {tail}", "slate", subject_text or "")
        if "description updated" in s or a == "updated":
            return _chip("desc updated", "blue", subject_text or "")
        return _chip(a or "email", "slate", subject_text or "")

    if rows:
        body_rows = []
        for r in rows:
            jid   = r.get("job_post_id") or "—"
            link  = r.get("view_job_link") or ""
            title = (r.get("job_title") or "").strip() or "—"
            org   = (r.get("posting_org") or "").strip()
            sfid  = (r.get("sf_job_id") or "").strip()
            tm    = r.get("et_time") or ""

            jid_html = (
                f'<a href="{link}" style="color:#2471a3;text-decoration:none;">#{jid}</a>'
                if link else f"#{jid}"
            )

            # Scrape result
            if r["scrape_ok"]:
                scrape_html = _chip("✓", "green", "title + description populated")
            elif r["stuck"]:
                scrape_html = _chip("stuck", "red",
                                    "Scrape produced no usable content and creation failed without recovery")
            elif r["auto_retried"] or r["manual_rescraped"]:
                scrape_html = _chip("retrying", "amber",
                                    "Initial scrape didn't populate fields; a retry/rescrape ran for this job")
            else:
                scrape_html = _chip("—", "slate", "No content row written for this email")

            # SF mapping — link to the Salesforce Lightning record when mapped
            # so the operator can open Kimedics (Job column) and SF side-by-side.
            if r["sf_mapped"]:
                sf_url = (
                    f"https://proxi.lightning.force.com/lightning/r/Job__c/{sfid}/view"
                    if sfid else ""
                )
                chip = _chip("✓", "green", f"Open Salesforce record · sf_job_id={sfid}")
                id_html = (
                    f'<span style="font-family:monospace;color:#2471a3;font-size:10px;">{sfid[:10]}…</span>'
                    if sfid else ""
                )
                inner = chip + (" " + id_html if id_html else "")
                sf_html = (
                    f'<a href="{sf_url}" style="text-decoration:none;">{inner}</a>'
                    if sf_url else inner
                )
            elif r["scrape_ok"]:
                sf_html = _chip("pending", "amber", "Scraped but no SF Job__c yet")
            else:
                sf_html = _chip("—", "slate", "Mapping deferred — no content")

            # SF Fields column: "+3 fields" or "—" or "new job"
            field_bits: list[str] = []
            if r["created_sf_job"]:
                field_bits.append(_chip("new SF job", "violet", "job_created_in_salesforce"))
            patches = int(r.get("fields_changed") or 0)
            if patches > 0:
                field_bits.append(_chip(f"+{patches} fields", "green",
                                        "Total Job__c fields patched in SF for this job"))
            if not field_bits:
                field_bits.append('<span style="color:#aaa;">—</span>')
            fields_html = " ".join(field_bits)

            # Notes — chips show amendments + actions for this email.
            notes: list[str] = []
            quar_n = int(r.get("fields_quarantined") or 0)
            rec_n  = int(r.get("push_recovered") or 0)
            err_n  = int(r.get("push_errors") or 0)
            err_unresolved = bool(r.get("push_error_unresolved"))
            if quar_n > 0:
                notes.append(_chip(
                    f"{quar_n} field dropped" if quar_n == 1 else f"{quar_n} fields dropped",
                    "amber",
                    "Salesforce rejected the value (length / type / picklist); recovery auto-dropped the field and re-pushed the rest",
                ))
            if rec_n > 0 and not err_unresolved:
                notes.append(_chip("push recovered", "cyan",
                                   "An SF push error was auto-recovered and the rest of the fields landed"))
            if err_unresolved:
                notes.append(_chip("push error", "red",
                                   "Salesforce field push errored and has not yet been recovered"))
            if r["ext_id_swap"]:
                notes.append(_chip("ID swap", "amber",
                                   "External_Job_ID__c was repointed on an existing SF record"))
            if r["auto_retried"]:
                notes.append(_chip("auto retry", "cyan", "Cron re-ran a previously failed scrape"))
            if r["manual_rescraped"]:
                notes.append(_chip("rescraped", "blue", "Operator hit Rescrape in /admin/recovery"))
            notes_html = " ".join(notes) if notes else '<span style="color:#aaa;">—</span>'

            org_html = f'<span style="color:#666;font-size:11px;">{org}</span>' if org else ""
            title_html = (
                f'<div style="color:#111;font-size:12px;">{title}</div>'
                + (f'<div>{org_html}</div>' if org_html else "")
            )

            email_kind = _email_kind_chip(r.get("subject", ""), r.get("action_or_change", ""))

            body_rows.append(
                "<tr>"
                f"<td class='mono' style='white-space:nowrap;'>{tm}</td>"
                f"<td style='white-space:nowrap;'>{email_kind}</td>"
                f"<td style='white-space:nowrap;'>{jid_html}</td>"
                f"<td>{title_html}</td>"
                f"<td style='text-align:center;'>{scrape_html}</td>"
                f"<td style='text-align:center;'>{sf_html}</td>"
                f"<td>{fields_html}</td>"
                f"<td>{notes_html}</td>"
                "</tr>"
            )

        emails_section = f"""
        <div class="section">
          <h2>Email-by-email outcome</h2>
          <p style="font-size:12px;color:#666;margin:0 0 8px;">
            One row per email received. Each row shows whether the job was scraped, whether SF was mapped,
            how many fields were patched, and any notable actions (ID swap, rescrape, stuck, etc.).
          </p>
          <table style="font-size:12px;width:100%;">
            <tr>
              <th>Time</th><th>Email</th><th>Job</th><th>Title / Org</th>
              <th>Scrape</th><th>SF</th><th>SF Fields</th><th>Notes</th>
            </tr>
            {''.join(body_rows)}
          </table>
        </div>"""
    else:
        emails_section = (
            '<div class="section"><h2>Email-by-email outcome</h2>'
            '<p style="color:#888;">No emails received in this period.</p></div>'
        )

    # ── Assemble body ─────────────────────────────────────────────────────────
    body = f"""
    <div class="section">
      <h2>Daily report — {period}</h2>
      <p style="margin:0 0 12px;">Status: {health_badge}</p>
      {stats_html}
    </div>

    {emails_section}

    <div class="section">
      <p style="font-size:12px;color:#888;margin:0;">
        Window: yesterday 5:00 AM–11:59 PM ET (full-day cutoff so reporting never spills into the next day).
        Each row corresponds to one email from Kimedics; the chips summarize what happened downstream
        in the scrape + Salesforce pipeline. Critical alerts (auth failures, parse crashes) are sent
        immediately when they occur.
      </p>
    </div>
    """

    # ── Plain-text fallback ───────────────────────────────────────────────────
    def _pt_row(r: dict) -> str:
        kind = r.get("action_or_change") or r.get("subject") or "?"
        scrape = "OK" if r["scrape_ok"] else ("STUCK" if r["stuck"] else "—")
        sf = "OK" if r["sf_mapped"] else ("pending" if r["scrape_ok"] else "—")
        patches = int(r.get("fields_changed") or 0)
        notes = []
        if r["created_sf_job"]: notes.append("new SF job")
        qn = int(r.get("fields_quarantined") or 0)
        if qn > 0:              notes.append(f"{qn} field dropped" if qn == 1 else f"{qn} fields dropped")
        if r.get("push_error_unresolved"): notes.append("push error")
        elif int(r.get("push_recovered") or 0) > 0: notes.append("push recovered")
        if r["ext_id_swap"]:    notes.append("ID swap")
        if r["auto_retried"]:   notes.append("auto retry")
        if r["manual_rescraped"]: notes.append("rescraped")
        notes_str = ", ".join(notes) if notes else ""
        title = (r.get("job_title") or "?")[:40]
        return (
            f"  {r.get('et_time',''):>10}  #{r.get('job_post_id','?'):<6}  "
            f"{kind:<22}  scrape={scrape:<7}  sf={sf:<8}  +{patches:>2}f  {notes_str}"
            f"  | {title}"
        )

    text = textwrap.dedent(f"""
    Proxi Daily Report — {period}
    {'='*72}
    Emails received        : {emails}
    Scraped OK             : {scraped_ok}
    SF Job__c mapped       : {sf_mapped}
    New SF job records     : {sf_jobs_created}
    New SF worksites       : {worksites_created}
    SF fields patched      : {patches_total}
    External_Job_ID swaps  : {ext_id_swaps}
    Auto retries           : {auto_retries}
    Manual rescrapes       : {manual_rescr}
    Stuck (needs fix)      : {stuck_jobs}
    Scrape failures        : {scrape_fails}

    Per-email outcome:
    """).strip() + "\n" + ("\n".join(_pt_row(r) for r in rows) if rows else "  (no emails)")

    return _send(
        subject,
        _html_wrap(
            "📊 Proxi Daily Report",
            f"{period} · Proxi Salesforce Automation",
            body,
        ),
        text,
    )


# ── Weekly client-facing pulse ──────────────────────────────────────────────────────

def send_weekly_summary(stats: dict) -> bool:
    """
    Client-facing weekly pulse — outcome-first framing for the head of a
    recruiting firm. Renders a self-contained HTML email that adapts to
    mobile widths and dark mode in modern clients (Apple Mail, iOS Mail,
    Gmail mobile app, Outlook iOS/Android). Skips sending when the week
    had zero email activity.
    """
    cur  = stats.get("current") or {}
    prv  = stats.get("previous") or {}
    period = stats.get("period_label", "Last week")

    emails    = int(cur.get("emails_received", 0))
    scraped   = int(cur.get("scraped_ok", 0))
    new_jobs  = int(cur.get("sf_jobs_created", 0))
    patches   = int(cur.get("field_patches_total", 0))
    needs     = int(cur.get("needs_attention", 0))
    latency   = cur.get("median_latency_min")
    top_prac  = cur.get("top_practices", [])
    top_jobs  = cur.get("top_jobs", [])
    hrs_saved = float(stats.get("hours_saved_estimate", 0.0))
    dollars_saved  = int(stats.get("dollars_saved_estimate", 0))
    hourly_rate    = int(stats.get("hourly_rate_usd", 80))
    manual_min     = int(stats.get("manual_baseline_min", 120))
    speed_x        = stats.get("speed_multiplier_x")
    top_states     = stats.get("top_states", [])
    states_total   = int(stats.get("states_total", 0))
    peak_day       = stats.get("peak_day")
    peak_day_count = int(stats.get("peak_day_count", 0))
    peak_hour      = stats.get("peak_hour")
    peak_hour_n    = int(stats.get("peak_hour_count", 0))
    trend          = stats.get("trend_weekly", [])
    cumulative     = stats.get("cumulative", {}) or {}
    narrative      = stats.get("narrative", "")
    series         = stats.get("daily_series", [])

    if emails == 0:
        print(f"[alert_email] Weekly pulse skipped — no emails in {period}")
        return False

    prv_emails  = int(prv.get("emails_received", 0))
    prv_patches = int(prv.get("field_patches_total", 0))
    prv_new     = int(prv.get("sf_jobs_created", 0))

    def _delta(cur_v, prev_v):
        if prev_v == 0: return None
        return round((cur_v - prev_v) / prev_v * 100)

    def _delta_html(pct: Optional[int]):
        if pct is None:
            return '<span class="dim" style="color:#a1a1aa;font-size:12px;">first reporting period</span>'
        if pct == 0:
            return '<span class="dim" style="color:#71717a;font-size:12px;">unchanged WoW</span>'
        color = "#16a34a" if pct > 0 else "#dc2626"
        arrow = "↑" if pct > 0 else "↓"
        return f'<span style="color:{color};font-size:12px;font-weight:600;">{arrow} {abs(pct)}% WoW</span>'

    coverage_pct = round(scraped / emails * 100) if emails else 0

    # Health framing — restrained palette.
    if needs == 0:
        health_text = "All systems healthy"
        health_dot  = "#16a34a"
        health_bg   = "#ecfdf5"
        health_fg   = "#15803d"
    elif needs <= 2:
        health_text = f"{needs} item{'s' if needs != 1 else ''} flagged for review"
        health_dot  = "#f59e0b"
        health_bg   = "#fffbeb"
        health_fg   = "#b45309"
    else:
        health_text = f"{needs} items flagged for review"
        health_dot  = "#dc2626"
        health_bg   = "#fef2f2"
        health_fg   = "#b91c1c"

    subject = f"Proxi Weekly Pulse · {period} · {emails} updates, {patches} SF changes, ~{hrs_saved:g} hrs saved"

    # ── Hero stat cell ───────────────────────────────────────────────────────
    def _hero(num, label, delta_html):
        return f"""
        <td class="card hero-cell" style="padding:20px 18px;background:#ffffff;border:1px solid #e4e4e7;border-radius:8px;width:33.33%;vertical-align:top;">
          <div class="dim" style="font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.06em;">{label}</div>
          <div class="num hero-num" style="font-size:38px;line-height:1.05;font-weight:700;color:#18181b;margin:6px 0 4px;letter-spacing:-0.02em;">{num}</div>
          <div>{delta_html}</div>
        </td>"""

    hero_row = f"""
    <table role="presentation" cellpadding="0" cellspacing="8" class="hero-stack" style="width:100%;border-collapse:separate;margin-top:8px;">
      <tr>
        {_hero(f"{emails:,}",   "Job updates ingested",   _delta_html(_delta(emails,  prv_emails)))}
        {_hero(f"{patches:,}",  "Salesforce field updates", _delta_html(_delta(patches, prv_patches)))}
        {_hero(f"{new_jobs:,}", "New roles added to SF",  _delta_html(_delta(new_jobs, prv_new)))}
      </tr>
    </table>"""

    # ── Narrative paragraph ─────────────────────────────────────────────────
    narrative_html = f"""
    <div class="card" style="margin:18px 0 0;padding:18px 22px;background:#fafafa;border:1px solid #e4e4e7;border-radius:8px;">
      <div class="text" style="font-size:14px;color:#27272a;line-height:1.55;">{narrative}</div>
    </div>""" if narrative else ""

    # ── ROI — week + cumulative + dollars ───────────────────────────────────
    cum_h     = float(cumulative.get("hours_saved", 0) or 0)
    cum_d     = int(cumulative.get("dollars_saved", 0) or 0)
    launch    = cumulative.get("launch_iso") or ""
    cum_eml   = int(cumulative.get("emails", 0) or 0)
    cum_pat   = int(cumulative.get("patches", 0) or 0)
    cum_field = int(cumulative.get("fields", 0) or 0)
    cum_new   = int(cumulative.get("new_jobs", 0) or 0)
    roi_html = ""
    if hrs_saved >= 1 or cum_h >= 1:
        roi_html = f"""
        <div class="roi" style="margin:18px 0 0;padding:22px 24px;background:#18181b;border-radius:8px;">
          <div style="color:#a1a1aa;font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;">Time recouped this week</div>
          <table role="presentation" cellpadding="0" cellspacing="0" class="duo-stack" style="width:100%;margin-top:6px;">
            <tr>
              <td style="vertical-align:top;width:50%;">
                <div style="color:#ffffff;font-size:40px;font-weight:700;line-height:1.0;letter-spacing:-0.02em;">~{hrs_saved:g} hrs</div>
                <div style="color:#a1a1aa;font-size:13px;margin-top:6px;">of manual Kimedics → Salesforce entry</div>
              </td>
              <td style="vertical-align:top;width:50%;padding-left:20px;">
                <div style="color:#ffffff;font-size:28px;font-weight:700;line-height:1.0;letter-spacing:-0.02em;">${dollars_saved:,}</div>
                <div style="color:#a1a1aa;font-size:13px;margin-top:6px;">recruiter capacity returned at ${hourly_rate}/hr</div>
              </td>
            </tr>
          </table>
        </div>"""

    # ── Speed advantage ─────────────────────────────────────────────────────
    speed_html = ""
    if latency is not None and speed_x:
        bar_max = max(manual_min, latency, 1)
        ours_pct   = max(2, int(latency / bar_max * 100))
        manual_pct = int(manual_min / bar_max * 100)
        speed_html = f"""
        <div class="card" style="margin:18px 0 0;padding:20px 22px;background:#ffffff;border:1px solid #e4e4e7;border-radius:8px;">
          <div class="dim" style="font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.06em;">Speed advantage</div>
          <div class="num" style="font-size:24px;font-weight:700;color:#18181b;margin:6px 0 4px;letter-spacing:-0.02em;">{speed_x}× faster than manual</div>
          <div class="muted" style="font-size:13px;color:#52525b;margin-bottom:14px;">
            Median time from Kimedics email to Salesforce: <strong style="color:#18181b;">{latency:.0f} min</strong>. Manual baseline: <strong style="color:#18181b;">~{manual_min} min</strong>.
          </div>
          <table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;">
            <tr>
              <td style="width:64px;font-size:12px;color:#52525b;font-weight:600;padding:4px 0;">Proxi</td>
              <td style="padding:4px 0;">
                <div style="height:12px;background:#f4f4f5;border-radius:6px;">
                  <div style="height:12px;width:{ours_pct}%;background:#27272a;border-radius:6px;"></div>
                </div>
              </td>
              <td style="width:84px;text-align:right;font-size:13px;color:#18181b;font-weight:700;padding:4px 0 4px 10px;">{latency:.0f} min</td>
            </tr>
            <tr>
              <td style="width:64px;font-size:12px;color:#52525b;font-weight:600;padding:4px 0;">Manual</td>
              <td style="padding:4px 0;">
                <div style="height:12px;background:#f4f4f5;border-radius:6px;">
                  <div style="height:12px;width:{manual_pct}%;background:#a1a1aa;border-radius:6px;"></div>
                </div>
              </td>
              <td style="width:84px;text-align:right;font-size:13px;color:#52525b;font-weight:700;padding:4px 0 4px 10px;">~{manual_min} min</td>
            </tr>
          </table>
        </div>"""

    # ── Editorial bar chart helper ──────────────────────────────────────────
    # Three-row table: [count] / [bar in fixed-height cell with hairline
    # baseline] / [label]. Monochrome zinc-800 fill — restrained, NYT-like.
    BAR_COLOR     = "#27272a"
    BAR_COLOR_DIM = "#d4d4d8"
    NUM_COLOR     = "#18181b"
    NUM_DIM       = "#a1a1aa"
    LABEL_COLOR   = "#71717a"
    BASELINE      = "#e4e4e7"

    def _bar_chart(items, bar_h: int = 92, bar_w: int = 36) -> str:
        max_v = max((c for _, c in items), default=0) or 1
        num_cells, bar_cells, lbl_cells = [], [], []
        for label, count in items:
            fill = max(3, int((count / max_v) * (bar_h - 8))) if count > 0 else 0
            num_cells.append(
                f'<td style="text-align:center;padding:0 4px 6px;font-size:13px;font-weight:600;'
                f'color:{NUM_COLOR if count > 0 else NUM_DIM};letter-spacing:-0.01em;font-variant-numeric:tabular-nums;">{count}</td>'
            )
            if count > 0:
                bar_inner = (
                    f'<div style="width:{bar_w}px;height:{fill}px;background:{BAR_COLOR};'
                    f'border-radius:3px 3px 0 0;margin:0 auto;"></div>'
                )
            else:
                bar_inner = (
                    f'<div style="width:{bar_w}px;height:2px;background:{BAR_COLOR_DIM};'
                    f'margin:0 auto;"></div>'
                )
            bar_cells.append(
                f'<td style="text-align:center;vertical-align:bottom;padding:0 4px;'
                f'height:{bar_h}px;border-bottom:1px solid {BASELINE};">{bar_inner}</td>'
            )
            lbl_cells.append(
                f'<td style="text-align:center;padding:8px 4px 0;font-size:11px;'
                f'color:{LABEL_COLOR};font-weight:500;letter-spacing:.02em;">{label}</td>'
            )
        return (
            '<table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;">'
            f'<tr>{"".join(num_cells)}</tr>'
            f'<tr>{"".join(bar_cells)}</tr>'
            f'<tr>{"".join(lbl_cells)}</tr>'
            '</table>'
        )

    # ── Daily activity ──────────────────────────────────────────────────────
    weekday_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    daily_items = [(wl, c) for (_d_iso, c), wl in zip(series, weekday_labels)]
    daily_section = f"""
    <div class="card" style="margin:18px 0 0;padding:20px 22px 16px;background:#ffffff;border:1px solid #e4e4e7;border-radius:8px;">
      <div class="dim" style="font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.06em;margin-bottom:16px;">Daily activity</div>
      {_bar_chart(daily_items, bar_h=92, bar_w=34)}
    </div>"""

    # ── 4-week trend ─────────────────────────────────────────────────────────
    trend_html = ""
    if trend:
        trend_chart = _bar_chart(list(trend), bar_h=80, bar_w=52)
        trend_html = f"""
        <div class="card" style="margin:18px 0 0;padding:20px 22px 16px;background:#ffffff;border:1px solid #e4e4e7;border-radius:8px;">
          <div class="dim" style="font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.06em;margin-bottom:16px;">4-week trend · job updates per week</div>
          {trend_chart}
        </div>"""

    # ── Reliability single line (not its own card) ───────────────────────────
    sla_bits = [f"<strong style='color:#18181b;'>{coverage_pct}%</strong> coverage"]
    if latency is not None:
        sla_bits.append(f"<strong style='color:#18181b;'>{latency:.0f} min</strong> median email→SF")
    sla_bits.append(
        f"<strong style='color:#18181b;'>{needs}</strong> needs attention" if needs
        else "<strong style='color:#18181b;'>0</strong> manual interventions"
    )
    reliability = f"""
    <div style="margin:18px 0 0;padding:14px 18px;background:{health_bg};border-radius:8px;display:block;">
      <div style="font-size:13px;color:{health_fg};font-weight:600;">
        <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{health_dot};margin-right:8px;vertical-align:middle;"></span>{health_text}
      </div>
      <div class="muted" style="font-size:13px;color:#52525b;margin-top:4px;">{' · '.join(sla_bits)}</div>
    </div>"""

    # ── US tile-map ──────────────────────────────────────────────────────────
    _US_TILES = [
        (0, 0, 'AK'),
        (0, 10, 'ME'),
        (1, 9, 'VT'), (1, 10, 'NH'),
        (2, 1, 'WA'), (2, 2, 'ID'), (2, 3, 'MT'), (2, 4, 'ND'),
        (2, 5, 'MN'), (2, 6, 'WI'), (2, 7, 'MI'),
        (2, 9, 'NY'), (2, 10, 'MA'), (2, 11, 'RI'),
        (3, 1, 'OR'), (3, 2, 'NV'), (3, 3, 'WY'), (3, 4, 'SD'),
        (3, 5, 'IA'), (3, 6, 'IL'), (3, 7, 'IN'), (3, 8, 'OH'),
        (3, 9, 'PA'), (3, 10, 'NJ'), (3, 11, 'CT'),
        (4, 1, 'CA'), (4, 2, 'UT'), (4, 3, 'CO'), (4, 4, 'NE'),
        (4, 5, 'MO'), (4, 6, 'KY'), (4, 7, 'WV'), (4, 8, 'VA'),
        (4, 9, 'MD'), (4, 10, 'DE'), (4, 11, 'DC'),
        (5, 3, 'AZ'), (5, 4, 'NM'), (5, 5, 'KS'), (5, 6, 'AR'),
        (5, 7, 'TN'), (5, 8, 'NC'), (5, 9, 'SC'),
        (6, 5, 'OK'), (6, 6, 'LA'), (6, 7, 'MS'), (6, 8, 'AL'), (6, 9, 'GA'),
        (7, 5, 'TX'), (7, 9, 'FL'),
        (8, 0, 'HI'), (8, 11, 'PR'),
    ]
    state_counts = {st: c for st, c in top_states}
    max_state = max(state_counts.values()) if state_counts else 1

    def _state_color(c: int) -> tuple[str, str]:
        """Monochrome zinc ramp — editorial, matches the bar charts."""
        if c == 0:
            return ("#fafafa", "#a1a1aa")
        ratio = c / max_state
        if ratio <= 0.25:
            return ("#e4e4e7", "#3f3f46")
        elif ratio <= 0.50:
            return ("#a1a1aa", "#18181b")
        elif ratio <= 0.75:
            return ("#52525b", "#ffffff")
        else:
            return ("#18181b", "#ffffff")

    grid = [[None] * 12 for _ in range(9)]
    for r, c, code in _US_TILES:
        grid[r][c] = code

    cell = 34
    rows_html = []
    for row in grid:
        cells = []
        for cell_code in row:
            if cell_code is None:
                cells.append(f'<td class="map-cell" style="width:{cell}px;height:{cell}px;padding:2px;"></td>')
            else:
                c = state_counts.get(cell_code, 0)
                bg, fg = _state_color(c)
                # On hover: numeric label via title attribute (most clients show on hover).
                title = f"{cell_code}: {c} update{'s' if c != 1 else ''}"
                # Show count only when ≥ 1; otherwise just the abbreviation in muted.
                inner = (
                    f'<div style="background:{bg};color:{fg};border-radius:4px;'
                    f'height:{cell}px;line-height:{cell}px;text-align:center;'
                    f'font-size:11px;font-weight:700;letter-spacing:.02em;">'
                    f'{cell_code}'
                    + (f' <span style="font-weight:600;opacity:.85;">{c}</span>' if c > 0 else "")
                    + '</div>'
                )
                cells.append(
                    f'<td class="map-cell" title="{title}" style="width:{cell}px;height:{cell}px;padding:2px;">{inner}</td>'
                )
        rows_html.append(f"<tr>{''.join(cells)}</tr>")

    legend_steps = [
        ("#fafafa", "0"),
        ("#e4e4e7", "1–25%"),
        ("#a1a1aa", "26–50%"),
        ("#52525b", "51–75%"),
        ("#18181b", "76–100%"),
    ]
    legend_cells = "".join(
        f'<td style="padding:0 6px;font-size:10px;color:#71717a;vertical-align:middle;">'
        f'<span style="display:inline-block;width:12px;height:12px;background:{bg};border-radius:2px;vertical-align:middle;margin-right:4px;"></span>{label}'
        f'</td>'
        for bg, label in legend_steps
    )

    map_html = f"""
    <div class="card" style="margin:18px 0 0;padding:20px 22px;background:#ffffff;border:1px solid #e4e4e7;border-radius:8px;">
      <div class="dim" style="font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.06em;">National footprint</div>
      <div class="num" style="font-size:24px;color:#18181b;font-weight:700;margin:6px 0 4px;letter-spacing:-0.02em;">{states_total} state{'s' if states_total != 1 else ''} active</div>
      <div class="muted" style="font-size:13px;color:#52525b;margin-bottom:14px;">Cell intensity scales with job-update volume; cell number is the count.</div>
      <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin:0 auto;">
        {''.join(rows_html)}
      </table>
      <table role="presentation" cellpadding="0" cellspacing="0" style="margin:14px auto 0;border-collapse:collapse;">
        <tr>{legend_cells}</tr>
      </table>
    </div>"""

    # ── Cadence: peak day + peak hour ────────────────────────────────────────
    cadence_html = ""
    if peak_day or peak_hour is not None:
        peak_hour_label = ""
        if peak_hour is not None:
            h12 = peak_hour % 12 or 12
            ampm = "AM" if peak_hour < 12 else "PM"
            peak_hour_label = f"{h12} {ampm} ET"
        cadence_html = f"""
        <table role="presentation" cellpadding="0" cellspacing="8" class="duo-stack" style="width:100%;border-collapse:separate;margin-top:10px;">
          <tr>
            <td class="card" style="padding:18px 20px;background:#ffffff;border:1px solid #e4e4e7;border-radius:8px;width:50%;vertical-align:top;">
              <div class="dim" style="font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.06em;">Peak day</div>
              <div class="num" style="font-size:22px;color:#18181b;font-weight:700;margin-top:4px;letter-spacing:-0.02em;">{peak_day or '—'}</div>
              <div class="muted" style="font-size:13px;color:#52525b;margin-top:2px;">{peak_day_count} update{'s' if peak_day_count != 1 else ''} received</div>
            </td>
            <td class="card" style="padding:18px 20px;background:#ffffff;border:1px solid #e4e4e7;border-radius:8px;width:50%;vertical-align:top;">
              <div class="dim" style="font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.06em;">Peak hour</div>
              <div class="num" style="font-size:22px;color:#18181b;font-weight:700;margin-top:4px;letter-spacing:-0.02em;">{peak_hour_label or '—'}</div>
              <div class="muted" style="font-size:13px;color:#52525b;margin-top:2px;">{peak_hour_n} update{'s' if peak_hour_n != 1 else ''} in that window</div>
            </td>
          </tr>
        </table>"""

    # ── Top practices (numbers prominent) ────────────────────────────────────
    top_prac_html = ""
    if top_prac:
        max_p = max(c for _, c in top_prac) or 1
        rows = []
        for name, count in top_prac:
            pct = int(count / max_p * 100)
            rows.append(
                f"""<tr>
                  <td style="padding:8px 0;font-size:13px;color:#18181b;width:55%;">{name}</td>
                  <td style="padding:8px 0;width:30%;">
                    <div style="height:8px;background:#f4f4f5;border-radius:4px;">
                      <div style="height:8px;width:{pct}%;background:#27272a;border-radius:4px;"></div>
                    </div>
                  </td>
                  <td style="padding:8px 0 8px 12px;font-size:13px;color:#18181b;font-weight:700;text-align:right;width:15%;">{count}</td>
                </tr>"""
            )
        top_prac_html = f"""
        <div class="card" style="margin:18px 0 0;padding:18px 22px;background:#ffffff;border:1px solid #e4e4e7;border-radius:8px;">
          <div class="dim" style="font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px;">Most active practices</div>
          <table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;">
            {''.join(rows)}
          </table>
        </div>"""

    # ── Top job records ──────────────────────────────────────────────────────
    top_jobs_html = ""
    if top_jobs:
        rows = []
        for j in top_jobs:
            sfid = j.get("sf_job_id") or ""
            sf_link = (
                f'<a href="https://proxi.lightning.force.com/lightning/r/Job__c/{sfid}/view" '
                f'style="color:#3f3f46;text-decoration:none;font-weight:600;border-bottom:1px solid #d4d4d8;">Open in Salesforce ↗</a>'
                if sfid else ""
            )
            title_org = (j.get("title") or "—") + (f' · <span class="muted" style="color:#52525b;">{j["org"]}</span>' if j.get("org") else "")
            rows.append(
                f"""<tr>
                  <td style="padding:10px 0;vertical-align:top;font-size:13px;width:16%;">
                    <a href="https://portal.kimedics.com/app/workspace/job-posts/{j['job_id']}" style="color:#18181b;text-decoration:none;font-weight:700;">#{j['job_id']}</a>
                  </td>
                  <td style="padding:10px 0;vertical-align:top;font-size:13px;color:#18181b;width:52%;">{title_org}</td>
                  <td style="padding:10px 0;vertical-align:top;font-size:13px;color:#18181b;font-weight:700;width:14%;text-align:right;">{j['patches']} fields</td>
                  <td style="padding:10px 0;vertical-align:top;font-size:12px;width:18%;text-align:right;">{sf_link}</td>
                </tr>"""
            )
        top_jobs_html = f"""
        <div class="card" style="margin:18px 0 0;padding:18px 22px;background:#ffffff;border:1px solid #e4e4e7;border-radius:8px;">
          <div class="dim" style="font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px;">Most updated job records</div>
          <table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;">
            {''.join(rows)}
          </table>
        </div>"""

    # ── All-time stats (small footer line) ───────────────────────────────────
    all_time_html = ""
    if launch and (cum_eml or cum_field):
        all_time_html = f"""
        <div class="muted" style="margin:18px 0 0;padding:14px 4px 0;border-top:1px solid #e4e4e7;font-size:11px;color:#71717a;line-height:1.6;">
          <span style="font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:#52525b;">All-time since {launch}</span> ·
          {cum_eml:,} emails processed ·
          {cum_field:,} SF field updates ·
          {cum_new:,} roles added to SF ·
          ~{cum_h:g} hrs returned (~${cum_d:,})
        </div>"""

    # ── Assemble full document ───────────────────────────────────────────────
    style_block = """
    <style>
      body { margin:0; padding:0; }
      a { color:#3f3f46; }
      /* Mobile: stack hero/duo cards, shrink chart cells. */
      @media only screen and (max-width: 600px) {
        .hero-stack > tbody > tr, .duo-stack > tbody > tr { display:block !important; }
        .hero-stack td, .duo-stack td { display:block !important; width:100% !important; box-sizing:border-box; }
        .hero-num { font-size:32px !important; }
        .map-cell { width:26px !important; height:26px !important; padding:1px !important; }
        .map-cell > div { height:26px !important; line-height:26px !important; font-size:10px !important; }
        .padded { padding:14px 16px !important; }
      }
      /* Dark mode: invert backgrounds and lift text.
         Inline ``background`` / ``color`` declarations need !important here
         so they win against the inline style attribute. */
      @media (prefers-color-scheme: dark) {
        body, .wrap-bg { background:#09090b !important; }
        .card { background:#18181b !important; border-color:#27272a !important; }
        .text, .num { color:#fafafa !important; }
        .muted { color:#a1a1aa !important; }
        .dim   { color:#71717a !important; }
        a { color:#d4d4d8 !important; }
        .roi  { background:#0a0a0c !important; border:1px solid #27272a; }
      }
    </style>
    """

    body_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <meta name="supported-color-schemes" content="light dark">
  <title>Proxi Weekly Pulse</title>
  {style_block}
</head>
<body style="margin:0;padding:0;background:#fafafa;color:#18181b;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <div class="wrap-bg" style="background:#fafafa;padding:24px 12px 32px;">
    <div style="max-width:720px;margin:0 auto;">
      <!-- Header -->
      <div style="padding:8px 4px 0;">
        <div class="dim" style="font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.08em;">Proxi · Weekly pulse</div>
        <div class="text" style="margin:6px 0 2px;font-size:24px;font-weight:700;color:#18181b;letter-spacing:-0.02em;">{period}</div>
      </div>

      {narrative_html}
      {hero_row}
      {roi_html}
      {speed_html}
      {daily_section}
      {trend_html}
      {reliability}
      {map_html}
      {cadence_html}
      {top_prac_html}
      {top_jobs_html}
      {all_time_html}

      <div class="muted" style="margin:24px 0 0;padding:12px 4px 0;font-size:11px;color:#a1a1aa;line-height:1.6;">
        Reporting window: {period} (US Eastern). Generated automatically — reply with questions.
      </div>
    </div>
  </div>
</body>
</html>"""

    # Plain-text fallback (concise; full table renders in HTML).
    daily_txt = "  " + "  ".join(f"{wl}:{c}" for (_, c), wl in zip(series, ["M","T","W","T","F","S","S"]))
    text = textwrap.dedent(f"""
    Proxi Weekly Pulse — {period}
    ============================================

    Job updates ingested    : {emails}    (prior week: {prv_emails})
    Salesforce field updates: {patches}   (prior week: {prv_patches})
    New roles added to SF   : {new_jobs}  (prior week: {prv_new})

    Time recouped this week : ~{hrs_saved:g} hours (~${dollars_saved:,} at ${hourly_rate}/hr)
    {f"All-time since {launch}: {cum_eml:,} emails · {cum_field:,} field updates · ~{cum_h:g} hrs returned (~${cum_d:,})" if launch else ""}

    Reliability             : {coverage_pct}% coverage{f" · median {latency:.0f} min email→SF" if latency is not None else ""} · {needs} need attention
    Speed                   : {f"{speed_x}× faster than manual" if speed_x else "—"}

    Daily activity:
    {daily_txt}

    States active: {states_total}
    Peak day    : {peak_day or '—'} ({peak_day_count})
    Peak hour   : {(peak_hour % 12 or 12) if peak_hour is not None else '—'} {'AM' if (peak_hour is not None and peak_hour < 12) else 'PM' if peak_hour is not None else ''} ET

    Most updated jobs:
    {chr(10).join(f"  · #{j['job_id']}  {j['title']}  ({j['patches']} field updates)" for j in top_jobs) or "  (none)"}

    Proxi · Kimedics → Salesforce automation
    """).strip()

    return _send(subject, body_html, text)


# ── Authentication failure alert ────────────────────────────────────────────────────

def send_authentication_failure_alert(
    failed_jobs: list[dict],
    total_jobs: int,
) -> bool:
    """
    Send an immediate alert when Kimedics authentication failures are detected.

    Args:
        failed_jobs: List of job dicts with authentication_failed=True
        total_jobs: Total number of jobs attempted in this batch

    Returns:
        True if email sent successfully, False otherwise
    """
    if not failed_jobs:
        return False

    failure_rate = len(failed_jobs) / total_jobs * 100 if total_jobs > 0 else 0

    # Build list of failed jobs
    failed_rows = []
    for job in failed_jobs[:20]:  # Limit to 20 for email size
        job_id = job.get("job_post_id", "Unknown")
        error = job.get("error", "Unknown error")
        link = job.get("view_job_link", "")

        link_html = f'<a href="{link}">View Job</a>' if link else "No link"
        failed_rows.append(
            f'<tr><td>#{job_id}</td><td>{error}</td><td>{link_html}</td></tr>'
        )

    # Critical alert badge
    health_badge = '<span class="badge critical">🔴 AUTHENTICATION FAILURE</span>'

    body = f"""
    <div class="section" style="background:#fef2f2;border:1px solid #dc2626;border-radius:4px;padding:16px;">
      <h2 style="color:#dc2626;margin:0 0 12px;">⚠️ CRITICAL: Kimedics Authentication Failed</h2>
      <p style="margin:0 0 8px;"><strong>{len(failed_jobs)} of {total_jobs} jobs ({failure_rate:.0f}%)</strong>
      failed to scrape due to authentication issues.</p>
      <p style="margin:0;color:#666;">The scraper detected "Sign Out" or similar UI elements, indicating we're not logged in.</p>
    </div>

    <div class="section">
      <h2>Failed Jobs</h2>
      <table style="width:100%;border-collapse:collapse;">
        <tr style="background:#f0f0f0;">
          <th style="text-align:left;padding:6px;">Job ID</th>
          <th style="text-align:left;padding:6px;">Error</th>
          <th style="text-align:left;padding:6px;">Link</th>
        </tr>
        {''.join(failed_rows)}
      </table>
      {f'<p style="font-size:12px;color:#888;">... and {len(failed_jobs) - 20} more</p>' if len(failed_jobs) > 20 else ''}
    </div>

    <div class="section">
      <h2>Required Actions</h2>
      <ol>
        <li><strong>Check Kimedics credentials</strong> in Modal secrets (KIMEDICS_EMAIL, KIMEDICS_PASSWORD)</li>
        <li><strong>Verify login flow</strong> hasn't changed on Kimedics portal</li>
        <li><strong>Test manual login</strong> to ensure account isn't locked</li>
        <li><strong>Review playwright selectors</strong> if login page structure changed</li>
      </ol>
    </div>

    <div class="section">
      <p style="font-size:12px;color:#888;margin:0;">
        This is a critical alert. The automation cannot function without valid authentication.
        Immediate action is required to restore service.
      </p>
    </div>
    """

    # Plain text version
    text = textwrap.dedent(f"""
    CRITICAL: Kimedics Authentication Failed
    {'='*50}

    {len(failed_jobs)} of {total_jobs} jobs ({failure_rate:.0f}%) failed due to authentication issues.

    The scraper detected "Sign Out" or similar UI elements, indicating we're not logged in.

    Failed Jobs:
    {chr(10).join(f'  - Job #{job.get("job_post_id", "?")} : {job.get("error", "Unknown")}' for job in failed_jobs[:10])}
    {f'  ... and {len(failed_jobs) - 10} more' if len(failed_jobs) > 10 else ''}

    Required Actions:
    1. Check Kimedics credentials in Modal secrets
    2. Verify login flow hasn't changed
    3. Test manual login to ensure account isn't locked
    4. Review playwright selectors if needed

    This is a critical alert requiring immediate attention.
    """).strip()

    subject = f"🚨 CRITICAL: Kimedics Authentication Failed - {len(failed_jobs)} jobs affected"

    return _send(
        subject,
        _html_wrap(
            "🚨 Authentication Failure Alert",
            f"{len(failed_jobs)} jobs failed · Proxi Automation",
            body,
        ),
        text,
    )
