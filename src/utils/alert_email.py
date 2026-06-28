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
from html import escape as _html_escape
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
# Client-facing recurring pulses (weekly + monthly) also go to the proxidocs
# stakeholders. The daily digest and all failure/date-review alerts stay on
# ALERT_RECIPIENTS only.
PULSE_RECIPIENTS = ALERT_RECIPIENTS + [
    "Peter.Wood@proxidocs.com",
    "cara.griffin@proxidocs.com",
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
  /* Stat grid — uses inline-block (not flex) for broad email-client support.
     Each box is 25% wide so 4 fit per row; remaining boxes wrap below.
     Long labels wrap inside the box instead of getting truncated. */
  .stat-row { font-size:0; margin-bottom:20px; line-height:0; }
  .stat-box { display:inline-block; vertical-align:top; box-sizing:border-box;
              width:calc(25% - 8px); margin:0 8px 8px 0;
              background:#f8f8f8; border:1px solid #e8e8e8; border-radius:6px;
              padding:14px 12px; text-align:center; font-size:14px; line-height:1.4; }
  .stat-box .num { font-size:26px; font-weight:700; color:#1a1a2e; line-height:1.1;
                   display:block; }
  .stat-box .lbl { font-size:10.5px; color:#888; margin-top:6px; text-transform:uppercase;
                   letter-spacing:.4px; word-break:break-word; line-height:1.3; display:block; }
  .footer { background:#f5f5f5; padding:14px 28px; font-size:11px; color:#999;
            border-top:1px solid #e8e8e8; }
  code { background:#f0f0f0; border-radius:3px; padding:1px 5px;
         font-family:monospace; font-size:12px; }
  .mono { font-family:monospace; font-size:12px; color:#555; }
  /* Mobile: stat boxes drop from 4-up to 2-up. Inner padding tightens so the
     table-rendered body doesn't introduce horizontal scrolling on phones. */
  @media only screen and (max-width: 600px) {
    .body { padding:16px !important; }
    .stat-box { width:calc(50% - 8px) !important; padding:12px 10px !important; }
    .stat-box .num { font-size:24px !important; }
    .stat-box .lbl { font-size:10px !important; }
    table { font-size:12px !important; }
    th, td { padding:5px 6px !important; }
  }
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
            if r.get("created_sf_worksite"):
                notes.append(_chip("new worksite", "cyan",
                                   "A Salesforce Worksite (Account) record was created for this job's practice/location"))
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
    cur    = stats.get("current") or {}
    prv    = stats.get("previous") or {}
    period = stats.get("period_label", "Last week")

    emails   = int(cur.get("emails_received", 0))
    cap_ok   = int(cur.get("content_ok", 0))
    cap_tot  = int(cur.get("content_emails", 0))
    cap_pass = emails - max(0, cap_tot - cap_ok)   # status pings pass; only true content misses count as errors
    opened   = int(cur.get("opened", 0))
    closed   = int(cur.get("closed", 0))
    patches  = int(cur.get("field_patches_total", 0))
    flagged  = cur.get("needs_attention_jobs", []) or []
    latency  = cur.get("mean_latency_min")  # outlier-trimmed average (headline)

    hrs_saved = float(stats.get("hours_saved_estimate", 0.0))
    hrs_prev  = float(stats.get("hours_saved_prev", 0.0))

    top_states    = stats.get("top_states", [])
    states_total  = int(stats.get("states_total", 0))
    cumulative    = stats.get("cumulative", {}) or {}
    lifecycle     = stats.get("lifecycle", {}) or {}

    if emails == 0:
        print(f"[alert_email] Weekly pulse skipped — no emails in {period}")
        return False

    prv_emails = int(prv.get("emails_received", 0))
    prv_open   = int(prv.get("opened", 0))
    prv_close  = int(prv.get("closed", 0))
    prv_patch  = int(prv.get("field_patches_total", 0))

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

    coverage_pct = round(cap_pass / emails * 100) if emails else 100

    def _fmt_dur(hours):
        """Human duration from a float number of hours."""
        if hours is None:
            return "—"
        if hours < 1:
            return f"{round(hours * 60)} min"
        if hours < 24:
            return f"{hours:.0f} hr" if abs(hours - round(hours)) < 0.05 else f"{hours:.1f} hr"
        days = hours / 24.0
        return f"{days:.1f} days"

    subject = f"Proxi Weekly Pulse · {period} · {emails:,} emails · {hrs_saved:g} hrs saved"

    # ── Top-line metrics, two rows: market outcomes + how well Proxi handled ─
    def _card_big(label, num, secondary):
        return f"""
        <td class="card hero-cell" style="padding:20px 18px;background:#ffffff;border:1px solid #e4e4e7;border-radius:12px;width:33.33%;vertical-align:top;">
          <div class="dim" style="font-size:10px;color:#71717a;font-weight:700;text-transform:uppercase;letter-spacing:.09em;line-height:1.3;">{label}</div>
          <div class="num hero-num" style="font-size:38px;line-height:1.0;font-weight:700;color:#18181b;margin:11px 0 9px;letter-spacing:-0.02em;white-space:nowrap;">{num}</div>
          <div>{secondary}</div>
        </td>"""

    def _card_sm(label, num, secondary):
        return f"""
        <td class="card hero-cell" style="padding:18px 16px;background:#ffffff;border:1px solid #e4e4e7;border-radius:12px;width:33.33%;vertical-align:top;">
          <div class="dim" style="font-size:10px;color:#71717a;font-weight:700;text-transform:uppercase;letter-spacing:.08em;line-height:1.3;">{label}</div>
          <div class="num" style="font-size:27px;line-height:1.05;font-weight:700;color:#18181b;margin:9px 0 7px;letter-spacing:-0.02em;white-space:nowrap;">{num}</div>
          <div>{secondary}</div>
        </td>"""

    def _delta_pill(pct):
        if pct is None:
            return '<span class="dim" style="font-size:11px;color:#a1a1aa;">first week</span>'
        if pct == 0:
            return '<span class="dim" style="font-size:11px;color:#71717a;">flat WoW</span>'
        cls   = "delta-up" if pct > 0 else "delta-down"
        arrow = "↑" if pct > 0 else "↓"
        return (f'<span class="{cls}" style="display:inline-block;border-radius:999px;'
                f'padding:3px 10px;font-size:11px;font-weight:700;">{arrow} {abs(pct)}% WoW</span>')

    def _sub(text):
        return f'<span class="muted" style="font-size:12px;color:#52525b;">{text}</span>'

    lat_txt = f"{latency:.1f} min" if latency is not None else "—"
    # Hiring-velocity metrics replace the misleading opened/closed event counts (see
    # send_impact_report): median time open + jobs that completed a full open→close
    # lifecycle this week (distinct jobs, not status emails).
    lc_week      = lifecycle.get("week", {}) or {}
    med_open_w   = _pulse_fmt_dur(lc_week.get("median_hours"))
    jobs_filled_w = int(lc_week.get("closed", 0))
    hero_row = f"""
    <table role="presentation" cellpadding="0" cellspacing="8" class="hero-stack" style="width:100%;border-collapse:separate;margin-top:8px;">
      <tr>
        {_card_big("Emails received", f"{emails:,}", _delta_pill(_delta(emails, prv_emails)))}
        {_card_big("Jobs filled",     f"{jobs_filled_w:,}", _sub("completed open → close"))}
        {_card_big("Median time open", med_open_w, _sub("typical open → filled"))}
      </tr>
    </table>
    <table role="presentation" cellpadding="0" cellspacing="8" class="hero-stack" style="width:100%;border-collapse:separate;margin-top:8px;">
      <tr>
        {_card_sm("SF field updates", f"{patches:,}",      _delta_pill(_delta(patches, prv_patch)))}
        {_card_sm("Capture rate",     f"{coverage_pct}%",  _sub(f"{cap_pass:,} of {emails:,} captured"))}
        {_card_sm("Avg sync time",    lat_txt,             _sub("email → Salesforce"))}
      </tr>
    </table>"""

    # ── Hours recouped (week + all-time; no dollars) ─────────────────────────
    cum_h     = float(cumulative.get("hours_saved", 0) or 0)
    launch    = cumulative.get("launch_iso") or ""
    cum_eml   = int(cumulative.get("emails", 0) or 0)
    cum_field = int(cumulative.get("fields", 0) or 0)
    hrs_delta = _delta_html(_delta(round(hrs_saved), round(hrs_prev)))
    spark = _pulse_trend(stats.get("hours_trend", []), "hrs saved per week")
    roi_html = ""
    if hrs_saved >= 1 or cum_h >= 1:
        all_time_txt = f"{cum_h:g} hrs all-time{f' since {launch}' if launch else ''}"
        spark_cell = (f'<td class="spark-cell" style="vertical-align:bottom;width:38%;padding-left:20px;">{spark}</td>'
                      if spark else "")
        roi_html = f"""
        <div class="roi" style="margin:18px 0 0;padding:22px 24px;background:#18181b;border-radius:8px;">
          <table role="presentation" cellpadding="0" cellspacing="0" class="duo-stack" style="width:100%;border-collapse:collapse;">
            <tr>
              <td style="vertical-align:top;">
                <div style="color:#a1a1aa;font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;">Manual time recouped this week</div>
                <div style="margin-top:10px;white-space:nowrap;">
                  <span style="color:#ffffff;font-size:48px;font-weight:700;line-height:1.0;letter-spacing:-0.02em;vertical-align:middle;">{hrs_saved:g} hrs</span>
                  <span style="margin-left:12px;vertical-align:middle;">{hrs_delta}</span>
                </div>
                <div style="color:#71717a;font-size:11px;margin-top:12px;">{all_time_txt}</div>
              </td>
              {spark_cell}
            </tr>
          </table>
        </div>"""

    # ── Chart helpers ────────────────────────────────────────────────────────
    # Bars/labels are class-driven so they invert correctly in dark mode.
    def _hist(items, bar_h=64, show_counts=True, label_keep=None, num_fs=11, show_zeros=True) -> str:
        # Value label sits INSIDE each bar's cell, bottom-aligned above the bar,
        # so it floats with the bar height. `reserve` caps the tallest bar so
        # bar + label always fits inside the cell — the chart can't overflow.
        max_v = max((c for _, c in items), default=0) or 1
        reserve = (num_fs + 6) if show_counts else 2
        bar_cells, lbl_cells = [], []
        for i, (label, count) in enumerate(items):
            fill = max(2, int(round((count / max_v) * (bar_h - reserve)))) if count > 0 else 2
            num_html = ""
            if show_counts and (count > 0 or show_zeros):
                num_html = (
                    f'<div class="chart-num" style="font-size:{num_fs}px;line-height:{num_fs + 3}px;'
                    f'height:{num_fs + 3}px;font-weight:600;font-variant-numeric:tabular-nums;">{count}</div>'
                )
            bar_cls = "bar" if count > 0 else "bar-zero"
            radius  = "border-radius:2px 2px 0 0;" if count > 0 else ""
            bar_cells.append(
                f'<td class="chart-base" style="text-align:center;vertical-align:bottom;padding:0 1px;height:{bar_h}px;">'
                f'{num_html}<div class="{bar_cls}" style="width:62%;height:{fill}px;margin:0 auto;{radius}"></div></td>'
            )
            keep = (label_keep is None) or (i in label_keep)
            lbl_cells.append(
                f'<td class="chart-lbl" style="text-align:center;padding:6px 1px 0;font-size:10px;font-weight:500;">{label if keep else ""}</td>'
            )
        return (f'<table role="presentation" cellpadding="0" cellspacing="0" '
                f'style="width:100%;border-collapse:collapse;table-layout:fixed;">'
                f'<tr>{"".join(bar_cells)}</tr><tr>{"".join(lbl_cells)}</tr></table>')

    # Job-flow (opened-vs-closed) card removed — these are email status-EVENTS, not job
    # state: opens are inflated by re-posts and closes are undercounted (jobs filled/expired
    # send no "Closed" email), so the comparison misleads. Omitted from the report.
    role_flow_html = ""

    # ── Flagged jobs to act on (only shown when something needs a human) ──────
    flags_html = ""
    if flagged:
        ids = " · ".join(
            f'<a href="https://portal.kimedics.com/app/workspace/job-posts/{j}" style="color:#b45309;font-weight:600;text-decoration:none;border-bottom:1px solid #fde68a;">#{j}</a>'
            for j in flagged[:12]
        )
        more = f" +{len(flagged) - 12} more" if len(flagged) > 12 else ""
        flags_html = f"""
        <div class="card" style="margin:18px 0 0;padding:16px 18px;background:#fffbeb;border:1px solid #fde68a;border-radius:8px;">
          <div style="font-size:13px;color:#92400e;font-weight:600;">{len(flagged)} job{'s' if len(flagged) != 1 else ''} need a look</div>
          <div style="font-size:13px;color:#b45309;margin-top:4px;line-height:1.6;">Salesforce sync didn't complete for: {ids}{more}</div>
        </div>"""

    # ── Lifecycle: this-week open duration + fast-close watch ────────────────
    lc_week = lifecycle.get("week", {}) or {}
    lc_all  = lifecycle.get("all", {}) or {}
    lifecycle_html = ""
    if lc_all.get("closed_total"):
        wk_opened  = int(lc_week.get("opened", 0))
        wk_closed  = int(lc_week.get("closed", 0))
        wk_lt1h    = int(lc_week.get("lt_1h", 0))
        wk_median  = _fmt_dur(lc_week.get("median_hours"))

        # All-time, demoted to a single reference line.
        all_ref = (
            f'Since launch: {_fmt_dur(lc_all.get("median_hours"))} median open across '
            f'{int(lc_all.get("closed_total", 0)):,} closed jobs.'
        )

        if wk_closed > 0:
            buckets = [
                ("Within 1 hr", wk_lt1h,                       int(lc_all.get("lt_1h", 0)),  "seg-close"),
                ("1–24 hrs",    int(lc_week.get("h1_24", 0)),  int(lc_all.get("h1_24", 0)),  "seg-upd"),
                ("1–7 days",    int(lc_week.get("d1_7", 0)),   int(lc_all.get("d1_7", 0)),   "seg-open"),
                ("Over 7 days", int(lc_week.get("gt_7d", 0)),  int(lc_all.get("gt_7d", 0)),  "bar-track"),
            ]
            tot     = wk_closed
            all_tot = int(lc_all.get("closed_total", 0)) or 1
            # Distribution bar as a FIXED-LAYOUT table — cells can never wrap; the
            # last segment omits its width so it absorbs any rounding remainder and
            # the bar always fills exactly 100%.
            def _seg_bar(vals, total, h):
                nz = [(n, cls) for n, cls in vals if n > 0]
                cells = ""
                for idx, (n, cls) in enumerate(nz):
                    w = "" if idx == len(nz) - 1 else f"width:{round(n / total * 100)}%;"
                    cells += f'<td class="{cls}" style="{w}height:{h}px;"></td>'
                return (f'<div style="border-radius:{h // 2 + 1}px;overflow:hidden;">'
                        '<table role="presentation" cellpadding="0" cellspacing="0" '
                        'style="width:100%;table-layout:fixed;border-collapse:collapse;">'
                        f'<tr>{cells}</tr></table></div>')
            week_bar = _seg_bar([(wn, cls) for _l, wn, _an, cls in buckets], tot, 14)
            all_bar  = _seg_bar([(an, cls) for _l, _wn, an, cls in buckets], all_tot, 8)
            _mini_lbl = ('font-size:10px;color:#a1a1aa;font-weight:600;'
                         'text-transform:uppercase;letter-spacing:.04em;')
            hdr = (
                '<tr>'
                '<td></td>'
                '<td class="dim" style="padding:0 0 4px;font-size:10px;color:#a1a1aa;font-weight:600;text-transform:uppercase;letter-spacing:.04em;text-align:right;">This week</td>'
                '<td class="dim" style="padding:0 0 4px 14px;font-size:10px;color:#a1a1aa;font-weight:600;text-transform:uppercase;letter-spacing:.04em;text-align:right;">All-time</td>'
                '</tr>'
            )
            legend_rows = hdr + "".join(
                f'<tr>'
                f'<td style="padding:5px 0;font-size:12px;white-space:nowrap;width:46%;">'
                f'<span class="{cls}" style="display:inline-block;width:9px;height:9px;border-radius:2px;vertical-align:middle;margin-right:6px;"></span>'
                f'<span class="text" style="color:#27272a;">{lbl}</span></td>'
                f'<td class="muted" style="padding:5px 0;font-size:12px;color:#52525b;text-align:right;width:27%;white-space:nowrap;font-variant-numeric:tabular-nums;">{wn} &nbsp;·&nbsp; {round(wn / tot * 100)}%</td>'
                f'<td class="muted" style="padding:5px 0 5px 10px;font-size:12px;color:#52525b;text-align:right;width:27%;white-space:nowrap;font-variant-numeric:tabular-nums;">{an:,} &nbsp;·&nbsp; {round(an / all_tot * 100)}%</td>'
                f'</tr>'
                for lbl, wn, an, cls in buckets
            )
            body = f"""
          <div class="num" style="font-size:24px;color:#18181b;font-weight:700;margin:6px 0 2px;letter-spacing:-0.02em;">{wk_median} median open</div>
          <div class="muted" style="font-size:13px;color:#52525b;margin-bottom:14px;">{wk_closed} of {wk_opened} jobs opened this week have closed</div>
          <div class="dim" style="{_mini_lbl}margin-bottom:5px;">This week</div>
          {week_bar}
          <div class="dim" style="{_mini_lbl}margin:11px 0 5px;">All-time</div>
          {all_bar}
          <table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;margin-top:14px;">{legend_rows}</table>"""
        else:
            body = f"""
          <div class="num" style="font-size:24px;color:#18181b;font-weight:700;margin:6px 0 2px;letter-spacing:-0.02em;">{wk_opened} jobs opened</div>
          <div class="muted" style="font-size:13px;color:#52525b;margin-bottom:6px;">none have closed yet this week</div>"""

        lifecycle_html = f"""
        <div class="card" style="margin:18px 0 0;padding:20px 22px;background:#ffffff;border:1px solid #e4e4e7;border-radius:8px;">
          <div class="dim" style="font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.06em;">How long jobs stayed open · this week vs all-time</div>
          {body}
          <div class="muted" style="margin-top:12px;font-size:11px;color:#71717a;line-height:1.5;">{all_ref}</div>
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

    def _state_level(c: int) -> int:
        """Intensity bucket 0–4; bg/fg per level are class-driven (dark-safe)."""
        if c <= 0:
            return 0
        ratio = c / max_state
        if ratio <= 0.25: return 1
        if ratio <= 0.50: return 2
        if ratio <= 0.75: return 3
        return 4

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
                continue
            c = state_counts.get(cell_code, 0)
            lvl = _state_level(c)
            title = f"{cell_code}: {c} email{'' if c == 1 else 's'}"
            if c > 0:
                # Abbreviation over count, two fixed-height lines — never wraps.
                inner = (
                    f'<div class="map-tile lvl{lvl}" style="height:{cell}px;border-radius:4px;text-align:center;overflow:hidden;">'
                    f'<div class="map-ab" style="font-size:8px;line-height:11px;font-weight:700;letter-spacing:.02em;padding-top:4px;">{cell_code}</div>'
                    f'<div class="map-ct" style="font-size:12px;line-height:13px;font-weight:700;">{c}</div>'
                    f'</div>'
                )
            else:
                inner = (
                    f'<div class="map-tile lvl0" style="height:{cell}px;line-height:{cell}px;border-radius:4px;'
                    f'text-align:center;font-size:9px;font-weight:700;letter-spacing:.02em;">{cell_code}</div>'
                )
            cells.append(
                f'<td class="map-cell" title="{title}" style="width:{cell}px;height:{cell}px;padding:2px;">{inner}</td>'
            )
        rows_html.append(f"<tr>{''.join(cells)}</tr>")

    # Qualitative ramp (none → more), shaded relative to the busiest state — NOT percentages,
    # which read as a misleading "share of national total". Matches the monthly pulse legend.
    legend_cells = "".join(
        f'<td style="padding:0 6px;font-size:10px;vertical-align:middle;">'
        f'<span class="{cls}" style="display:inline-block;width:12px;height:12px;border-radius:2px;vertical-align:middle;margin-right:4px;"></span>'
        f'<span class="dim" style="color:#71717a;">{label}</span></td>'
        for cls, label in [("lvl0", "none"), ("lvl1", "fewer"), ("lvl2", ""), ("lvl3", ""), ("lvl4", "more")]
    )

    map_html = f"""
    <div class="card" style="margin:18px 0 0;padding:20px 22px;background:#ffffff;border:1px solid #e4e4e7;border-radius:8px;">
      <div class="dim" style="font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.06em;">National footprint</div>
      <div class="num" style="font-size:24px;color:#18181b;font-weight:700;margin:6px 0 4px;letter-spacing:-0.02em;">{states_total} state{'s' if states_total != 1 else ''} active</div>
      <div class="muted" style="font-size:13px;color:#52525b;margin-bottom:14px;">This week's emails by state.</div>
      <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin:0 auto;table-layout:fixed;">
        {''.join(rows_html)}
      </table>
      <table role="presentation" cellpadding="0" cellspacing="0" style="margin:14px auto 0;border-collapse:collapse;">
        <tr>{legend_cells}</tr>
      </table>
    </div>"""

    # ── All-time stats (small footer line; no dollars) ───────────────────────
    all_time_html = ""
    if launch and (cum_eml or cum_field):
        all_time_html = f"""
        <div class="muted" style="margin:18px 0 0;padding:14px 4px 0;border-top:1px solid #e4e4e7;font-size:11px;color:#71717a;line-height:1.6;">
          <span style="font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:#52525b;">All-time since {launch}</span> ·
          {cum_eml:,} emails ·
          {cum_field:,} field updates ·
          {cum_h:g} hrs returned
        </div>"""

    # ── Assemble full document ───────────────────────────────────────────────
    style_block = """
    <style>
      body { margin:0; padding:0; }
      a { color:#3f3f46; }

      /* Charts (light) — class-driven so dark mode can invert them. */
      .chart-num  { color:#18181b; }
      .chart-lbl  { color:#71717a; }
      .chart-base { border-bottom:1px solid #e4e4e7; }
      .bar        { background:#27272a; }
      .bar-zero   { background:#d4d4d8; }
      .bar-track  { background:#d4d4d8; }
      .seg-open   { background:#6e9b7e; }   /* muted green  */
      .seg-upd    { background:#897fa8; }   /* muted purple */
      .seg-close  { background:#b07f93; }   /* dusty rose   */

      /* WoW pill badges */
      .delta-up   { background:#ecfdf5; color:#15803d; }
      .delta-down { background:#fef2f2; color:#b91c1c; }

      /* Map tiles (light) — bg + fg per intensity level. */
      .lvl0 { background:#f4f4f5; color:#a1a1aa; }
      .lvl1 { background:#e4e4e7; color:#3f3f46; }
      .lvl2 { background:#a1a1aa; color:#18181b; }
      .lvl3 { background:#52525b; color:#ffffff; }
      .lvl4 { background:#18181b; color:#ffffff; }

      /* Mobile: stack hero cards, shrink map tiles. */
      @media only screen and (max-width: 600px) {
        .hero-stack > tbody > tr, .duo-stack > tbody > tr { display:block !important; }
        .hero-stack > tbody > tr > td, .duo-stack > tbody > tr > td { display:block !important; width:100% !important; box-sizing:border-box; }
        .hero-num { font-size:32px !important; }
        .spark-cell { padding-left:0 !important; padding-top:18px !important; }
        .map-cell { width:26px !important; height:26px !important; padding:1px !important; }
        .map-tile { height:26px !important; }
        .map-tile.lvl0 { line-height:26px !important; }
        .map-ab { font-size:7px !important; line-height:9px !important; padding-top:3px !important; }
        .map-ct { font-size:10px !important; line-height:11px !important; }
      }

      /* Dark mode: invert backgrounds, charts, and map ramp.
         Inline declarations need !important to win over the style attribute. */
      @media (prefers-color-scheme: dark) {
        body, .wrap-bg { background:#09090b !important; }
        .card { background:#18181b !important; border-color:#27272a !important; }
        .text, .num { color:#fafafa !important; }
        .muted { color:#a1a1aa !important; }
        .dim   { color:#71717a !important; }
        a { color:#d4d4d8 !important; }
        .roi  { background:#0a0a0c !important; border:1px solid #27272a; }

        .chart-num  { color:#fafafa !important; }
        .chart-base { border-bottom-color:#3f3f46 !important; }
        .bar        { background:#d4d4d8 !important; }
        .bar-zero   { background:#3f3f46 !important; }
        .bar-track  { background:#3f3f46 !important; }
        .seg-open   { background:#85b394 !important; }   /* muted green  */
        .seg-upd    { background:#a79dc4 !important; }   /* muted purple */
        .seg-close  { background:#c79aac !important; }   /* dusty rose   */
        .delta-up   { background:#052e1b !important; color:#4ade80 !important; }
        .delta-down { background:#3a0d0d !important; color:#f87171 !important; }

        .lvl0 { background:#27272a !important; color:#52525b !important; }
        .lvl1 { background:#3f3f46 !important; color:#d4d4d8 !important; }
        .lvl2 { background:#52525b !important; color:#fafafa !important; }
        .lvl3 { background:#a1a1aa !important; color:#18181b !important; }
        .lvl4 { background:#fafafa !important; color:#18181b !important; }

        .lc-flag { background:#1c1917 !important; border-color:#78350f !important; }
        .lc-flag strong { color:#fbbf24 !important; }
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

      {hero_row}
      {roi_html}
      {role_flow_html}
      {flags_html}
      {lifecycle_html}
      {map_html}
      {all_time_html}

      <div class="muted" style="margin:24px 0 0;padding:12px 4px 0;font-size:11px;color:#a1a1aa;line-height:1.6;">
        Reporting window: {period} (US Eastern). Generated automatically — reply with questions.
      </div>
    </div>
  </div>
</body>
</html>"""

    # Plain-text fallback (concise; full charts render in HTML).
    lat_line = f"{latency:.1f} min average" if latency is not None else "—"
    lc_all_t = lifecycle.get("all", {}) or {}
    median_all = _fmt_dur(lc_all_t.get("median_hours"))
    text = textwrap.dedent(f"""
    Proxi Weekly Pulse — {period}
    ============================================

    Emails received : {emails}   (prior week: {prv_emails})
    Jobs opened     : {opened}   (prior week: {prv_open})
    Jobs closed     : {closed}   (prior week: {prv_close})
    SF field updates: {patches}  (prior week: {prv_patch})
    Capture rate    : {coverage_pct}% ({cap_pass} of {emails} captured)
    Avg sync time   : {lat_line}
    Manual time recouped : {hrs_saved:g} hrs this week · {cum_h:g} hrs all-time

    Open duration (all-time): {median_all} median across {int(lc_all_t.get('closed_total', 0)):,} closed jobs
    States active: {states_total}

    Proxi · Kimedics → Salesforce automation
    """).strip()

    return _send(subject, body_html, text, recipients=PULSE_RECIPIENTS)


# ── Shared pulse rendering helpers (weekly + monthly) ────────────────────────
# NOTE: send_weekly_summary still carries inline copies of these; the weekly can
# later be pointed at these module-level versions for a 1-line dedupe.

_PULSE_STYLE = """
    <style>
      body { margin:0; padding:0; }
      a { color:#3f3f46; }
      .chart-num  { color:#18181b; }
      .chart-lbl  { color:#71717a; }
      .chart-base { border-bottom:1px solid #e4e4e7; }
      .bar        { background:#27272a; }
      .bar-zero   { background:#d4d4d8; }
      .bar-track  { background:#d4d4d8; }
      .seg-open   { background:#6e9b7e; }
      .seg-upd    { background:#897fa8; }
      .seg-close  { background:#b07f93; }
      .delta-up   { background:#ecfdf5; color:#15803d; }
      .delta-down { background:#fef2f2; color:#b91c1c; }
      .pill-live  { background:#ecfdf5; color:#15803d; }
      .pill-build { background:#fffbeb; color:#b45309; }
      .note-box   { background:#fafafa; }
      .lvl0 { background:#f4f4f5; color:#a1a1aa; }
      .lvl1 { background:#e4e4e7; color:#3f3f46; }
      .lvl2 { background:#a1a1aa; color:#18181b; }
      .lvl3 { background:#52525b; color:#ffffff; }
      .lvl4 { background:#18181b; color:#ffffff; }
      @media only screen and (max-width: 600px) {
        .hero-stack > tbody > tr, .duo-stack > tbody > tr { display:block !important; }
        .hero-stack > tbody > tr > td, .duo-stack > tbody > tr > td { display:block !important; width:100% !important; box-sizing:border-box; }
        .hero-num { font-size:32px !important; }
        .spark-cell { padding-left:0 !important; padding-top:18px !important; }
        .map-cell { width:26px !important; height:26px !important; padding:1px !important; }
        .map-tile { height:26px !important; }
        .map-tile.lvl0 { line-height:26px !important; }
        .map-ab { font-size:7px !important; line-height:9px !important; padding-top:3px !important; }
        .map-ct { font-size:10px !important; line-height:11px !important; }
      }
      @media (prefers-color-scheme: dark) {
        body, .wrap-bg { background:#09090b !important; }
        .card { background:#18181b !important; border-color:#27272a !important; }
        .text, .num { color:#fafafa !important; }
        .muted { color:#a1a1aa !important; }
        .dim   { color:#71717a !important; }
        a { color:#d4d4d8 !important; }
        .roi  { background:#0a0a0c !important; border:1px solid #27272a; }
        .chart-num  { color:#fafafa !important; }
        .chart-base { border-bottom-color:#3f3f46 !important; }
        .bar        { background:#d4d4d8 !important; }
        .bar-zero   { background:#3f3f46 !important; }
        .bar-track  { background:#3f3f46 !important; }
        .seg-open   { background:#85b394 !important; }
        .seg-upd    { background:#a79dc4 !important; }
        .seg-close  { background:#c79aac !important; }
        .delta-up   { background:#052e1b !important; color:#4ade80 !important; }
        .delta-down { background:#3a0d0d !important; color:#f87171 !important; }
        .pill-live  { background:#052e1b !important; color:#4ade80 !important; }
        .pill-build { background:#3a2a06 !important; color:#fbbf24 !important; }
        .note-box   { background:#27272a !important; }
        .lvl0 { background:#27272a !important; color:#52525b !important; }
        .lvl1 { background:#3f3f46 !important; color:#d4d4d8 !important; }
        .lvl2 { background:#52525b !important; color:#fafafa !important; }
        .lvl3 { background:#a1a1aa !important; color:#18181b !important; }
        .lvl4 { background:#fafafa !important; color:#18181b !important; }
      }
    </style>
    """

_US_TILES = [
    (0, 0, 'AK'), (0, 10, 'ME'),
    (1, 9, 'VT'), (1, 10, 'NH'),
    (2, 1, 'WA'), (2, 2, 'ID'), (2, 3, 'MT'), (2, 4, 'ND'), (2, 5, 'MN'), (2, 6, 'WI'), (2, 7, 'MI'),
    (2, 9, 'NY'), (2, 10, 'MA'), (2, 11, 'RI'),
    (3, 1, 'OR'), (3, 2, 'NV'), (3, 3, 'WY'), (3, 4, 'SD'), (3, 5, 'IA'), (3, 6, 'IL'), (3, 7, 'IN'),
    (3, 8, 'OH'), (3, 9, 'PA'), (3, 10, 'NJ'), (3, 11, 'CT'),
    (4, 1, 'CA'), (4, 2, 'UT'), (4, 3, 'CO'), (4, 4, 'NE'), (4, 5, 'MO'), (4, 6, 'KY'), (4, 7, 'WV'),
    (4, 8, 'VA'), (4, 9, 'MD'), (4, 10, 'DE'), (4, 11, 'DC'),
    (5, 3, 'AZ'), (5, 4, 'NM'), (5, 5, 'KS'), (5, 6, 'AR'), (5, 7, 'TN'), (5, 8, 'NC'), (5, 9, 'SC'),
    (6, 5, 'OK'), (6, 6, 'LA'), (6, 7, 'MS'), (6, 8, 'AL'), (6, 9, 'GA'),
    (7, 5, 'TX'), (7, 9, 'FL'), (8, 0, 'HI'), (8, 11, 'PR'),
]


def _pulse_fmt_dur(hours):
    if hours is None:
        return "—"
    if hours < 1:
        return f"{round(hours * 60)} min"
    if hours < 24:
        return f"{hours:.0f} hr" if abs(hours - round(hours)) < 0.05 else f"{hours:.1f} hr"
    return f"{hours / 24.0:.1f} days"


def _pulse_delta(cur_v, prev_v):
    if prev_v == 0:
        return None
    return round((cur_v - prev_v) / prev_v * 100)


def _pulse_delta_pill(pct, unit="WoW"):
    if pct is None:
        return '<span class="dim" style="font-size:11px;color:#a1a1aa;">first period</span>'
    if pct == 0:
        return f'<span class="dim" style="font-size:11px;color:#71717a;">flat {unit}</span>'
    cls = "delta-up" if pct > 0 else "delta-down"
    arrow = "↑" if pct > 0 else "↓"
    return (f'<span class="{cls}" style="display:inline-block;border-radius:999px;'
            f'padding:3px 10px;font-size:11px;font-weight:700;">{arrow} {abs(pct)}% {unit}</span>')


def _pulse_delta_text(pct, unit="WoW"):
    if pct is None:
        return '<span class="dim" style="color:#a1a1aa;font-size:12px;">first reporting period</span>'
    if pct == 0:
        return f'<span class="dim" style="color:#71717a;font-size:12px;">unchanged {unit}</span>'
    color = "#16a34a" if pct > 0 else "#dc2626"
    arrow = "↑" if pct > 0 else "↓"
    return f'<span style="color:{color};font-size:12px;font-weight:600;">{arrow} {abs(pct)}% {unit}</span>'


def _pulse_sub(text):
    return f'<span class="muted" style="font-size:12px;color:#52525b;">{text}</span>'


def _pulse_card_big(label, num, secondary, width="33.33%"):
    return f"""
        <td class="card hero-cell" style="padding:20px 18px;background:#ffffff;border:1px solid #e4e4e7;border-radius:12px;width:{width};vertical-align:top;">
          <div class="dim" style="font-size:10px;color:#71717a;font-weight:700;text-transform:uppercase;letter-spacing:.09em;line-height:1.3;">{label}</div>
          <div class="num hero-num" style="font-size:38px;line-height:1.0;font-weight:700;color:#18181b;margin:11px 0 9px;letter-spacing:-0.02em;white-space:nowrap;">{num}</div>
          <div>{secondary}</div>
        </td>"""


def _pulse_card_sm(label, num, secondary):
    return f"""
        <td class="card hero-cell" style="padding:18px 16px;background:#ffffff;border:1px solid #e4e4e7;border-radius:12px;width:33.33%;vertical-align:top;">
          <div class="dim" style="font-size:10px;color:#71717a;font-weight:700;text-transform:uppercase;letter-spacing:.08em;line-height:1.3;">{label}</div>
          <div class="num" style="font-size:27px;line-height:1.05;font-weight:700;color:#18181b;margin:9px 0 7px;letter-spacing:-0.02em;white-space:nowrap;">{num}</div>
          <div>{secondary}</div>
        </td>"""


def _pulse_hist(items, bar_h=64, show_counts=True, label_keep=None, num_fs=11, show_zeros=True):
    max_v = max((c for _, c in items), default=0) or 1
    reserve = (num_fs + 6) if show_counts else 2
    bar_cells, lbl_cells = [], []
    for i, (label, count) in enumerate(items):
        fill = max(2, int(round((count / max_v) * (bar_h - reserve)))) if count > 0 else 2
        num_html = ""
        if show_counts and (count > 0 or show_zeros):
            num_html = (f'<div class="chart-num" style="font-size:{num_fs}px;line-height:{num_fs + 3}px;'
                        f'height:{num_fs + 3}px;font-weight:600;font-variant-numeric:tabular-nums;">{count}</div>')
        bar_cls = "bar" if count > 0 else "bar-zero"
        radius = "border-radius:2px 2px 0 0;" if count > 0 else ""
        bar_cells.append(
            f'<td class="chart-base" style="text-align:center;vertical-align:bottom;padding:0 1px;height:{bar_h}px;">'
            f'{num_html}<div class="{bar_cls}" style="width:62%;height:{fill}px;margin:0 auto;{radius}"></div></td>')
        keep = (label_keep is None) or (i in label_keep)
        lbl_cells.append(
            f'<td class="chart-lbl" style="text-align:center;padding:6px 1px 0;font-size:10px;font-weight:500;">{label if keep else ""}</td>')
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" '
            f'style="width:100%;border-collapse:collapse;table-layout:fixed;">'
            f'<tr>{"".join(bar_cells)}</tr><tr>{"".join(lbl_cells)}</tr></table>')


def _pulse_stacked(series_split, labels, bar_h=120, show_totals=True, label_keep=None, num_fs=11):
    totals = [v["opened"] + v["updated"] + v["closed"] for _, v in series_split]
    max_v = (max(totals) if totals else 0) or 1
    reserve = (num_fs + 5) if show_totals else 2
    pad = 1 if len(series_split) >= 12 else 3      # dense charts (24h) get tight columns
    bar_cells, lbl_cells = [], []
    for i, (_d, v) in enumerate(series_split):
        tot = v["opened"] + v["updated"] + v["closed"]
        if tot > 0:
            col_h = max(3, int(round((tot / max_v) * (bar_h - reserve))))
            o = int(round(v["opened"] / tot * col_h))
            u = int(round(v["updated"] / tot * col_h))
            c = max(0, col_h - o - u)
            stack = f'<div style="width:64%;margin:0 auto;height:{col_h}px;border-radius:3px 3px 0 0;overflow:hidden;">'
            if o: stack += f'<div class="seg-open"  style="height:{o}px;"></div>'
            if u: stack += f'<div class="seg-upd"   style="height:{u}px;"></div>'
            if c: stack += f'<div class="seg-close" style="height:{c}px;"></div>'
            stack += "</div>"
        else:
            stack = '<div class="bar-zero" style="width:64%;height:2px;margin:0 auto;"></div>'
        num = ""
        if show_totals and tot > 0:
            num = (f'<div class="chart-num" style="font-size:{num_fs}px;line-height:{num_fs + 3}px;'
                   f'height:{num_fs + 3}px;font-weight:600;font-variant-numeric:tabular-nums;">{tot}</div>')
        bar_cells.append(
            f'<td class="chart-base" style="text-align:center;vertical-align:bottom;padding:0 {pad}px;height:{bar_h}px;">{num}{stack}</td>')
        keep = (label_keep is None) or (i in label_keep)
        lbl = labels[i] if (i < len(labels) and keep) else ""
        lbl_cells.append(
            f'<td class="chart-lbl" style="text-align:center;padding:6px {pad}px 0;font-size:10px;font-weight:500;">{lbl}</td>')
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" '
            f'style="width:100%;border-collapse:collapse;table-layout:fixed;">'
            f'<tr>{"".join(bar_cells)}</tr><tr>{"".join(lbl_cells)}</tr></table>')


def _pulse_line_chart(series, w=280, h=84):
    """White line chart as a rendered image (QuickChart / Chart.js). Renders in the
    browser preview AND email clients including Gmail (which strip inline SVG). Single
    white line, only the last point dotted; no axes/grid. (White, not red — the trend
    is a positive metric and red reads as negative.)

    The image carries its OWN dark background (not transparent): Gmail's dark theme
    inverts the surrounding card's HTML background to light but can't recolor image
    pixels, so a transparent white line would vanish on the flipped-light card. A solid
    dark backing keeps the line legible in every client/mode."""
    import json, urllib.parse
    pts = [(l, v) for l, v in series if v is not None]
    if len(pts) < 2:
        return ""
    data = [v for _, v in pts]
    cfg = {
        "type": "line",
        "data": {
            "labels": [l for l, _ in pts],
            "datasets": [{
                "data": data,
                "borderColor": "#ffffff",
                "borderWidth": 2,
                "pointRadius": [0] * (len(data) - 1) + [4],
                "pointBackgroundColor": "#ffffff",
                "fill": False,
                "lineTension": 0.35,
            }],
        },
        "options": {
            "legend": {"display": False},
            "layout": {"padding": {"top": 6, "right": 8, "left": 2, "bottom": 2}},
            # Anchor at 0 so low periods don't visually read as "dropping to zero".
            "scales": {"xAxes": [{"display": False}],
                       "yAxes": [{"display": False, "ticks": {"beginAtZero": True}}]},
        },
    }
    c = urllib.parse.quote(json.dumps(cfg, separators=(",", ":")))
    # Solid dark backing (matches the ROI card) so the white line survives client dark-mode
    # inversion; rounded so it reads as an intentional inset chart if the card flips light.
    url = f"https://quickchart.io/chart?bkg=%2318181b&w={w * 2}&h={h * 2}&c={c}"
    return (f'<img src="{url}" width="{w}" height="{h}" alt="Hours saved trend" '
            f'style="display:block;width:100%;max-width:{w}px;height:auto;border-radius:6px;">')


def _pulse_trend(series, caption=""):
    """White line chart for the ROI card: the last point's value floats top-right,
    and a caption underneath says what the line is (so it isn't mistaken for a
    cumulative/running total)."""
    img = _pulse_line_chart(series)
    if not img:
        return ""
    last = series[-1][1] or 0
    label = (f'<div style="font-size:12px;line-height:14px;color:#ffffff;font-weight:700;'
             f'text-align:right;font-variant-numeric:tabular-nums;margin-bottom:4px;">{last:g} hrs</div>')
    cap = (f'<div style="font-size:10px;color:#71717a;text-align:right;margin-top:5px;'
           f'text-transform:uppercase;letter-spacing:.04em;">{caption}</div>') if caption else ""
    return label + img + cap


def _pulse_seg_bar(vals, total, h):
    nz = [(n, cls) for n, cls in vals if n > 0]
    cells = ""
    for idx, (n, cls) in enumerate(nz):
        w = "" if idx == len(nz) - 1 else f"width:{round(n / total * 100)}%;"
        cells += f'<td class="{cls}" style="{w}height:{h}px;"></td>'
    return (f'<div style="border-radius:{h // 2 + 1}px;overflow:hidden;">'
            '<table role="presentation" cellpadding="0" cellspacing="0" '
            'style="width:100%;table-layout:fixed;border-collapse:collapse;">'
            f'<tr>{cells}</tr></table></div>')


def _pulse_us_map(top_states, states_total, caption):
    state_counts = {st: c for st, c in top_states}
    max_state = max(state_counts.values()) if state_counts else 1

    def _lvl(c):
        if c <= 0:
            return 0
        r = c / max_state
        return 1 if r <= 0.25 else 2 if r <= 0.50 else 3 if r <= 0.75 else 4

    grid = [[None] * 12 for _ in range(9)]
    for r, c, code in _US_TILES:
        grid[r][c] = code
    cell = 34
    rows_html = []
    for row in grid:
        cells = []
        for code in row:
            if code is None:
                cells.append(f'<td class="map-cell" style="width:{cell}px;height:{cell}px;padding:2px;"></td>')
                continue
            c = state_counts.get(code, 0)
            lvl = _lvl(c)
            title = f"{code}: {c} job{'' if c == 1 else 's'}"
            if c > 0:
                inner = (f'<div class="map-tile lvl{lvl}" style="height:{cell}px;border-radius:4px;text-align:center;overflow:hidden;">'
                         f'<div class="map-ab" style="font-size:8px;line-height:11px;font-weight:700;letter-spacing:.02em;padding-top:4px;">{code}</div>'
                         f'<div class="map-ct" style="font-size:12px;line-height:13px;font-weight:700;">{c}</div></div>')
            else:
                inner = (f'<div class="map-tile lvl0" style="height:{cell}px;line-height:{cell}px;border-radius:4px;'
                         f'text-align:center;font-size:9px;font-weight:700;letter-spacing:.02em;">{code}</div>')
            cells.append(f'<td class="map-cell" title="{title}" style="width:{cell}px;height:{cell}px;padding:2px;">{inner}</td>')
        rows_html.append(f"<tr>{''.join(cells)}</tr>")
    # Shading is RELATIVE to the busiest state (count ÷ top-state count), not a share of the
    # national total — so the labels read as a fewer→more gradient, not as percentages that
    # invite "but no state is 76% of all jobs" confusion.
    legend = "".join(
        f'<td style="padding:0 6px;font-size:10px;vertical-align:middle;">'
        f'<span class="{cls}" style="display:inline-block;width:12px;height:12px;border-radius:2px;vertical-align:middle;margin-right:4px;"></span>'
        f'<span class="dim" style="color:#71717a;">{label}</span></td>'
        for cls, label in [("lvl0", "none"), ("lvl1", "fewer"), ("lvl2", ""), ("lvl3", ""), ("lvl4", "more")]
    )
    return f"""
    <div class="card" style="margin:18px 0 0;padding:20px 22px;background:#ffffff;border:1px solid #e4e4e7;border-radius:8px;">
      <div class="dim" style="font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.06em;">National footprint</div>
      <div class="num" style="font-size:24px;color:#18181b;font-weight:700;margin:6px 0 4px;letter-spacing:-0.02em;">{states_total} state{'s' if states_total != 1 else ''} active</div>
      <div class="muted" style="font-size:13px;color:#52525b;margin-bottom:14px;">{caption}</div>
      <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin:0 auto;table-layout:fixed;">{''.join(rows_html)}</table>
      <table role="presentation" cellpadding="0" cellspacing="0" style="margin:14px auto 0;border-collapse:collapse;"><tr>{legend}</tr></table>
      <div class="muted" style="font-size:11px;color:#a1a1aa;text-align:center;margin-top:8px;line-height:1.5;">Darker = more jobs, shaded relative to the busiest state.</div>
    </div>"""


def _pulse_doc(kicker, period, body_html, title):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <meta name="supported-color-schemes" content="light dark">
  <title>{title}</title>
  {_PULSE_STYLE}
</head>
<body style="margin:0;padding:0;background:#fafafa;color:#18181b;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <div class="wrap-bg" style="background:#fafafa;padding:24px 12px 32px;">
    <div style="max-width:720px;margin:0 auto;">
      <div style="padding:8px 4px 0;">
        <div class="dim" style="font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.08em;">{kicker}</div>
        <div class="text" style="margin:6px 0 2px;font-size:24px;font-weight:700;color:#18181b;letter-spacing:-0.02em;">{period}</div>
      </div>
      {body_html}
      <div class="muted" style="margin:24px 0 0;padding:12px 4px 0;font-size:11px;color:#a1a1aa;line-height:1.6;">
        Reporting window: {period} (US Eastern). Generated automatically — reply with questions.
      </div>
    </div>
  </div>
</body>
</html>"""


def send_monthly_summary(stats: dict) -> bool:
    """
    Client-facing monthly pulse — the comprehensive overview. Same metric set as
    the weekly but over the prior calendar month, with month-over-month deltas
    and the richer chart set (activity by week, throughput, day/hour cadence,
    open-duration distribution, footprint). Skips sending on a zero-email month.
    """
    cur    = stats.get("current") or {}
    prv    = stats.get("previous") or {}
    period = stats.get("period_label", "Last month")

    emails  = int(cur.get("emails_received", 0))
    cap_ok  = int(cur.get("content_ok", 0))
    cap_tot = int(cur.get("content_emails", 0))
    cap_pass = emails - max(0, cap_tot - cap_ok)   # status pings pass; only true content misses count as errors
    opened  = int(cur.get("opened", 0))
    updated = int(cur.get("updated", 0))
    closed  = int(cur.get("closed", 0))
    patches = int(cur.get("field_patches_total", 0))
    latency = cur.get("mean_latency_min")

    hrs_saved = float(stats.get("hours_saved_estimate", 0.0))
    hrs_prev  = float(stats.get("hours_saved_prev", 0.0))
    top_states    = stats.get("top_states", [])
    states_total  = int(stats.get("states_total", 0))
    weekday_split = stats.get("weekday_split", [])
    hour_split    = stats.get("hour_split", [])
    monthly_trend = stats.get("monthly_trend", [])
    cumulative    = stats.get("cumulative", {}) or {}
    lifecycle     = stats.get("lifecycle", {}) or {}

    if emails == 0:
        print(f"[alert_email] Monthly pulse skipped — no emails in {period}")
        return False

    prv_emails = int(prv.get("emails_received", 0))
    prv_open   = int(prv.get("opened", 0))
    prv_close  = int(prv.get("closed", 0))
    prv_patch  = int(prv.get("field_patches_total", 0))
    coverage_pct = round(cap_pass / emails * 100) if emails else 100
    MoM = "MoM"

    subject = f"Proxi Monthly Pulse · {period} · {emails:,} emails · {hrs_saved:g} hrs saved"

    lat_txt = f"{latency:.1f} min" if latency is not None else "—"

    # Hiring-velocity metrics replace the misleading opened/closed event counts (see
    # send_impact_report): median time a job stays open + jobs that completed a full
    # open→close lifecycle this month (distinct jobs, not status emails).
    lc_month     = lifecycle.get("week", {}) or {}
    med_open_m   = _pulse_fmt_dur(lc_month.get("median_hours"))
    jobs_filled_m = int(lc_month.get("closed", 0))
    hero_row = f"""
    <table role="presentation" cellpadding="0" cellspacing="8" class="hero-stack" style="width:100%;border-collapse:separate;margin-top:8px;">
      <tr>
        {_pulse_card_big("Emails received", f"{emails:,}", _pulse_delta_pill(_pulse_delta(emails, prv_emails), MoM))}
        {_pulse_card_big("Jobs filled",     f"{jobs_filled_m:,}", _pulse_sub("completed open → close"))}
        {_pulse_card_big("Median time open", med_open_m, _pulse_sub("typical open → filled"))}
      </tr>
    </table>
    <table role="presentation" cellpadding="0" cellspacing="8" class="hero-stack" style="width:100%;border-collapse:separate;margin-top:8px;">
      <tr>
        {_pulse_card_sm("SF field updates", f"{patches:,}",     _pulse_delta_pill(_pulse_delta(patches, prv_patch), MoM))}
        {_pulse_card_sm("Capture rate",     f"{coverage_pct}%", _pulse_sub(f"{cap_pass:,} of {emails:,} captured"))}
        {_pulse_card_sm("Avg sync time",    lat_txt,            _pulse_sub("email → Salesforce"))}
      </tr>
    </table>"""

    cum_h     = float(cumulative.get("hours_saved", 0) or 0)
    launch    = cumulative.get("launch_iso") or ""
    cum_eml   = int(cumulative.get("emails", 0) or 0)
    cum_field = int(cumulative.get("fields", 0) or 0)
    hrs_delta = _pulse_delta_text(_pulse_delta(round(hrs_saved), round(hrs_prev)), MoM)
    spark = _pulse_trend(stats.get("hours_trend", []), "hrs saved per month")
    roi_html = ""
    if hrs_saved >= 1 or cum_h >= 1:
        spark_cell = (f'<td class="spark-cell" style="vertical-align:bottom;width:38%;padding-left:20px;">{spark}</td>'
                      if spark else "")
        roi_html = f"""
        <div class="roi" style="margin:18px 0 0;padding:22px 24px;background:#18181b;border-radius:8px;">
          <table role="presentation" cellpadding="0" cellspacing="0" class="duo-stack" style="width:100%;border-collapse:collapse;">
            <tr>
              <td style="vertical-align:top;">
                <div style="color:#a1a1aa;font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;">Manual time recouped this month</div>
                <div style="margin-top:10px;white-space:nowrap;">
                  <span style="color:#ffffff;font-size:48px;font-weight:700;line-height:1.0;letter-spacing:-0.02em;vertical-align:middle;">{hrs_saved:g} hrs</span>
                  <span style="margin-left:12px;vertical-align:middle;">{hrs_delta}</span>
                </div>
                <div style="color:#71717a;font-size:11px;margin-top:14px;">{cum_h:g} hrs all-time{f' since {launch}' if launch else ''}</div>
              </td>
              {spark_cell}
            </tr>
          </table>
        </div>"""

    # ── Activity by month — the strategic trajectory ─────────────────────────
    monthly_trend_html = ""
    if monthly_trend:
        monthly_trend_html = f"""
        <div class="card" style="margin:18px 0 0;padding:20px 22px 16px;background:#ffffff;border:1px solid #e4e4e7;border-radius:8px;">
          <div class="dim" style="font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.06em;margin-bottom:16px;">Activity by month · emails handled</div>
          {_pulse_hist(list(monthly_trend), bar_h=84)}
        </div>"""

    # Job-flow (opened-vs-closed) card removed — see send_weekly_summary: these are email
    # status-EVENTS, not job state (inflated opens, undercounted closes), so omitted.
    role_flow_html = ""

    # When work happens: weekday + hour totals (plain bars, no open/update/close split).
    cadence_html = ""
    if weekday_split or hour_split:
        wtot = [(l, v["opened"] + v["updated"] + v["closed"]) for l, v in weekday_split]
        htot = [(f"{h % 12 or 12}{'a' if h < 12 else 'p'}", v["opened"] + v["updated"] + v["closed"])
                for h, v in hour_split]
        keep_hours = {i for i, (h, _v) in enumerate(hour_split) if h in (0, 6, 12, 18)}
        cadence_html = f"""
        <div class="card" style="margin:18px 0 0;padding:20px 22px 16px;background:#ffffff;border:1px solid #e4e4e7;border-radius:8px;">
          <div class="dim" style="font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.06em;margin-bottom:14px;">By day of week · Mon–Fri</div>
          {_pulse_hist(wtot, bar_h=92)}
          <div class="dim" style="font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.06em;margin:22px 0 14px;">By hour (ET)</div>
          {_pulse_hist(htot, bar_h=72, show_counts=True, label_keep=keep_hours, num_fs=9, show_zeros=False)}
        </div>"""

    # Lifecycle: open-duration distribution for the month.
    lc_m   = lifecycle.get("week", {}) or {}
    lc_all = lifecycle.get("all", {}) or {}
    lifecycle_html = ""
    if lc_all.get("closed_total"):
        m_opened = int(lc_m.get("opened", 0))
        m_closed = int(lc_m.get("closed", 0))
        m_median = _pulse_fmt_dur(lc_m.get("median_hours"))
        all_ref = (f'Since launch: {_pulse_fmt_dur(lc_all.get("median_hours"))} median open across '
                   f'{int(lc_all.get("closed_total", 0)):,} closed jobs.')
        if m_closed > 0:
            buckets = [
                ("Within 1 hr", int(lc_m.get("lt_1h", 0)),  int(lc_all.get("lt_1h", 0)),  "seg-close"),
                ("1–24 hrs",    int(lc_m.get("h1_24", 0)),  int(lc_all.get("h1_24", 0)),  "seg-upd"),
                ("1–7 days",    int(lc_m.get("d1_7", 0)),   int(lc_all.get("d1_7", 0)),   "seg-open"),
                ("Over 7 days", int(lc_m.get("gt_7d", 0)),  int(lc_all.get("gt_7d", 0)),  "bar-track"),
            ]
            mtot   = m_closed
            alltot = int(lc_all.get("closed_total", 0)) or 1
            month_bar = _pulse_seg_bar([(mn, cls) for _l, mn, _an, cls in buckets], mtot, 14)
            all_bar   = _pulse_seg_bar([(an, cls) for _l, _mn, an, cls in buckets], alltot, 8)
            mini = 'font-size:10px;color:#a1a1aa;font-weight:600;text-transform:uppercase;letter-spacing:.04em;'
            hdr = ('<tr><td></td>'
                   '<td class="dim" style="padding:0 0 4px;font-size:10px;color:#a1a1aa;font-weight:600;text-transform:uppercase;letter-spacing:.04em;text-align:right;">This month</td>'
                   '<td class="dim" style="padding:0 0 4px 10px;font-size:10px;color:#a1a1aa;font-weight:600;text-transform:uppercase;letter-spacing:.04em;text-align:right;">All-time</td></tr>')
            rows = hdr + "".join(
                f'<tr>'
                f'<td style="padding:5px 0;font-size:12px;white-space:nowrap;width:46%;">'
                f'<span class="{cls}" style="display:inline-block;width:9px;height:9px;border-radius:2px;vertical-align:middle;margin-right:6px;"></span>'
                f'<span class="text" style="color:#27272a;">{lbl}</span></td>'
                f'<td class="muted" style="padding:5px 0;font-size:12px;color:#52525b;text-align:right;width:27%;white-space:nowrap;font-variant-numeric:tabular-nums;">{mn} &nbsp;·&nbsp; {round(mn / mtot * 100)}%</td>'
                f'<td class="muted" style="padding:5px 0 5px 10px;font-size:12px;color:#52525b;text-align:right;width:27%;white-space:nowrap;font-variant-numeric:tabular-nums;">{an:,} &nbsp;·&nbsp; {round(an / alltot * 100)}%</td>'
                f'</tr>'
                for lbl, mn, an, cls in buckets
            )
            lifecycle_html = f"""
        <div class="card" style="margin:18px 0 0;padding:20px 22px;background:#ffffff;border:1px solid #e4e4e7;border-radius:8px;">
          <div class="dim" style="font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.06em;">Hiring velocity · how long jobs stayed open · this month vs all-time</div>
          <div class="num" style="font-size:24px;color:#18181b;font-weight:700;margin:6px 0 2px;letter-spacing:-0.02em;">{m_median} median open</div>
          <div class="muted" style="font-size:13px;color:#52525b;margin-bottom:14px;">{m_closed} of {m_opened} jobs opened this month have closed</div>
          <div class="dim" style="{mini}margin-bottom:5px;">This month</div>
          {month_bar}
          <div class="dim" style="{mini}margin:11px 0 5px;">All-time</div>
          {all_bar}
          <table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;margin-top:14px;">{rows}</table>
          <div class="muted" style="margin-top:12px;font-size:11px;color:#71717a;line-height:1.5;">{all_ref}</div>
        </div>"""

    map_html = _pulse_us_map(top_states, states_total, "This month's emails by state.")

    all_time_html = ""
    if launch and (cum_eml or cum_field):
        all_time_html = f"""
        <div class="muted" style="margin:18px 0 0;padding:14px 4px 0;border-top:1px solid #e4e4e7;font-size:11px;color:#71717a;line-height:1.6;">
          <span style="font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:#52525b;">All-time since {launch}</span> ·
          {cum_eml:,} emails · {cum_field:,} field updates · {cum_h:g} hrs returned
        </div>"""

    # Ordered by client impact: value (ROI) → pipeline (job flow) → growth
    # trajectory → fill speed → reach → operational rhythm (cadence) last.
    body_inner = (hero_row + roi_html + role_flow_html + monthly_trend_html
                  + lifecycle_html + map_html + cadence_html + all_time_html)
    body_html = _pulse_doc("Proxi · Monthly pulse", period, body_inner, "Proxi Monthly Pulse")

    lat_line = f"{latency:.1f} min average" if latency is not None else "—"
    text = textwrap.dedent(f"""
    Proxi Monthly Pulse — {period}
    ============================================

    Emails received : {emails}   (prior month: {prv_emails})
    Jobs opened     : {opened}   (prior month: {prv_open})
    Jobs closed     : {closed}   (prior month: {prv_close})
    SF field updates: {patches}  (prior month: {prv_patch})
    Capture rate    : {coverage_pct}% ({cap_pass} of {emails} captured)
    Avg sync time   : {lat_line}
    Manual time recouped : {hrs_saved:g} hrs this month · {cum_h:g} hrs all-time

    States active: {states_total}

    Proxi · Kimedics → Salesforce automation
    """).strip()

    return _send(subject, body_html, text, recipients=PULSE_RECIPIENTS)


def _pulse_section_header(title, status_text, kind):
    """Divider between automations: bold name on the left, status pill on the right.
    kind: 'live' (green) or 'build' (amber)."""
    dot = "#16a34a" if kind == "live" else "#d97706"
    pill_cls = "pill-live" if kind == "live" else "pill-build"
    return f"""
    <div style="margin:32px 0 0;padding:0 4px;">
      <table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;">
        <tr>
          <td style="vertical-align:middle;">
            <span class="text" style="font-size:19px;font-weight:700;color:#18181b;letter-spacing:-0.01em;">{title}</span>
          </td>
          <td style="vertical-align:middle;text-align:right;white-space:nowrap;">
            <span class="{pill_cls}" style="display:inline-block;border-radius:999px;padding:4px 12px;font-size:11px;font-weight:700;letter-spacing:.02em;">
              <span style="display:inline-block;width:7px;height:7px;border-radius:999px;background:{dot};vertical-align:middle;margin-right:6px;"></span>{status_text}</span>
          </td>
        </tr>
      </table>
    </div>"""


def _impact_djc_section(djc=None):
    """DJC → Salesforce results block. Driven by real scrape KPIs from the DJC
    pipeline's dry-run period (no Salesforce writes — scraped/processed only)."""
    d = djc or {}
    cand   = int(d.get("candidates", 0))
    cvs    = int(d.get("cvs_parsed", 0))
    newc   = int(d.get("net_new", 0))
    dup    = int(d.get("already_in_sf", 0))
    unc    = int(d.get("uncontactable", 0))
    contact = int(d.get("contactable", 0))
    states = int(d.get("states", 0))
    specs  = int(d.get("specialties", 0))
    cv_only = int(d.get("cv_only_contact", 0))
    runs   = int(d.get("runs", 0))
    days   = int(d.get("days", 0))
    start  = d.get("start_disp", d.get("start", ""))
    end    = d.get("end_disp", d.get("end", ""))
    cov    = d.get("coverage") or {}

    intro = (f"Sources dental candidates from Dentist Job Cafe and creates Salesforce <strong>Contacts</strong> "
             f"with the résumé attached. It has been running daily in <strong>read-only (dry-run) mode</strong> — "
             f"fully scraping and processing candidates without writing to Salesforce yet. "
             f"Here's what it did over {days} days ({start} – {end}):")

    hero_big = f"""
    <table role="presentation" cellpadding="0" cellspacing="8" class="hero-stack" style="width:100%;border-collapse:separate;margin-top:12px;">
      <tr>
        {_pulse_card_big("Candidates scraped",  f"{cand:,}", _pulse_sub(f"across {specs} dental specialties"))}
        {_pulse_card_big("Résumés parsed",      f"{cvs:,}",  _pulse_sub("in memory — nothing stored"))}
        {_pulse_card_big("Net-new for Salesforce", f"{newc:,}", _pulse_sub("deduped &amp; contactable"))}
      </tr>
    </table>"""
    hero_sm = f"""
    <table role="presentation" cellpadding="0" cellspacing="8" class="hero-stack" style="width:100%;border-collapse:separate;margin-top:8px;">
      <tr>
        {_pulse_card_sm("Already in Salesforce", f"{dup:,}",     _pulse_sub("matched &amp; skipped"))}
        {_pulse_card_sm("Contact recovered",     f"{contact:,}", _pulse_sub("phone and/or email"))}
        {_pulse_card_sm("States covered",        f"{states}",    _pulse_sub("national reach"))}
      </tr>
    </table>"""

    # Funnel — what the pipeline did with every scraped candidate.
    funnel_html = ""
    if cand:
        segs = [(dup, "seg-upd", "Already in Salesforce"),
                (unc, "bar-track", "Uncontactable — skipped"),
                (newc, "seg-open", "Net-new, ready for SF")]
        bar = _pulse_seg_bar([(n, cls) for n, cls, _l in segs], cand or 1, 14)
        rows = "".join(
            f'<tr>'
            f'<td style="padding:5px 0;font-size:12px;white-space:nowrap;width:62%;">'
            f'<span class="{cls}" style="display:inline-block;width:9px;height:9px;border-radius:2px;vertical-align:middle;margin-right:6px;"></span>'
            f'<span class="text" style="color:#27272a;">{lbl}</span></td>'
            f'<td class="muted" style="padding:5px 0;font-size:12px;color:#52525b;text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums;">{n:,} &nbsp;·&nbsp; {round(n / cand * 100)}%</td>'
            f'</tr>'
            for n, cls, lbl in segs
        )
        funnel_html = f"""
        <div style="margin:18px 0 0;">
          <div class="dim" style="font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.06em;margin-bottom:12px;">What the pipeline did with all {cand:,} candidates</div>
          {bar}
          <table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;margin-top:12px;">{rows}</table>
        </div>"""

    # Salesforce coverage — does the scrape catch who SF flags as active on DJC?
    coverage_html = ""
    if cov:
        P = int(cov.get("population", 0)); C = int(cov.get("covered", 0))
        pct = int(cov.get("pct", 0)); ceil = int(cov.get("ceiling", 0))
        was = int(cov.get("was_pct", 0)); recovered = int(cov.get("recovered", 0))
        win = int(cov.get("window_days", 7)); ref = cov.get("ref", "")
        gap = round(ceil / P * 100) if P else 0
        csegs = [(C, "seg-open", "Captured"),
                 (ceil, "bar-track", "By-design ceiling (out-of-window)")]
        cbar = _pulse_seg_bar([(n, cls) for n, cls, _l in csegs if n], P or 1, 14)
        crows = "".join(
            f'<tr>'
            f'<td style="padding:5px 0;font-size:12px;white-space:nowrap;width:66%;">'
            f'<span class="{cls}" style="display:inline-block;width:9px;height:9px;border-radius:2px;vertical-align:middle;margin-right:6px;"></span>'
            f'<span class="text" style="color:#27272a;">{lbl}</span></td>'
            f'<td class="muted" style="padding:5px 0;font-size:12px;color:#52525b;text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums;">{n} &nbsp;·&nbsp; {round(n / P * 100)}%</td>'
            f'</tr>'
            for n, cls, lbl in csegs if n
        )
        rose = (f'<span class="delta-up" style="display:inline-block;border-radius:999px;padding:3px 10px;'
                f'font-size:11px;font-weight:700;margin-left:8px;">↑ from {was}%</span>') if was else ""
        coverage_html = f"""
        <div class="card" style="margin:18px 0 0;padding:20px 22px;background:#ffffff;border:1px solid #e4e4e7;border-radius:8px;">
          <div class="dim" style="font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.06em;">Salesforce coverage · candidates SF flags active on DJC (last {win} days)</div>
          <div style="margin:8px 0 4px;">
            <span class="num" style="font-size:30px;font-weight:700;color:#18181b;letter-spacing:-0.02em;">{pct}%</span>
            <span class="muted" style="font-size:13px;color:#52525b;">covered</span>{rose}
          </div>
          <div class="muted" style="font-size:13px;color:#52525b;margin-bottom:14px;">{C} of {P} candidates Salesforce marks active on DJC in the trailing {win} days are in the scrape (as of {ref}). Viewed-collision fix confirmed live in production.</div>
          {cbar}
          <table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;margin-top:12px;">{crows}</table>
          <div class="note-box muted" style="margin-top:14px;padding:14px 16px;border-radius:6px;font-size:12px;color:#52525b;line-height:1.6;">
            A <strong class="text" style="color:#27272a;">viewed-collision fix is now live in production</strong> — {recovered} candidates the old viewed-flag filter was silently dropping are recovered via the new DB-dedup selection, lifting coverage {was}% → {pct}%.
            The remaining {ceil} (~{gap}%) are a <strong class="text" style="color:#27272a;">by-design ceiling</strong>: their real DJC activity is older than {win} days, while Salesforce's "Active on DJC" is a manually-entered date — so a {win}-day-activity scrape can't reach them. At {pct}%, this is effectively full coverage of the population the automation is designed to capture; widening the activity window (14/30 days) is the only further lever, a scope decision rather than a defect.
          </div>
        </div>"""

    steps = [
        f"Ran <strong>{runs} automated passes</strong> across {specs} dental roles/specialties, scraping under fixed filters (recent activity, exclude visa).",
        f"Recovered contact info from the profile, or parsed the résumé in memory with Claude when missing — CV parsing alone made <strong>{cv_only:,}</strong> otherwise-uncontactable candidates reachable.",
        "Deduped every candidate against Salesforce on phone <em>or</em> email <em>or</em> name + profile link, skipping anyone already on file.",
        "For each net-new candidate, assembled the full Contact payload + résumé — ready to create the moment writes are switched on.",
    ]
    bullets = "".join(
        f'<tr><td style="vertical-align:top;padding:4px 8px 4px 0;color:#a1a1aa;font-size:13px;">•</td>'
        f'<td class="text" style="padding:4px 0;font-size:13px;line-height:1.55;color:#27272a;">{s}</td></tr>'
        for s in steps
    )
    return f"""
    <div class="card" style="margin:14px 0 0;padding:20px 22px;background:#ffffff;border:1px solid #e4e4e7;border-radius:8px;">
      <div class="text" style="font-size:14px;line-height:1.6;color:#27272a;">{intro}</div>
      {hero_big}{hero_sm}{funnel_html}{coverage_html}
      <div class="dim" style="font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.06em;margin:22px 0 8px;">How it works</div>
      <table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;">{bullets}</table>
      <div class="note-box muted" style="margin-top:16px;padding:14px 16px;border-radius:6px;font-size:12px;color:#52525b;line-height:1.6;">
        <strong class="text" style="color:#27272a;">Status:</strong> running daily in read-only mode — <strong class="text" style="color:#27272a;">zero Salesforce writes by design</strong>.
        The scrape, contact-recovery, CV-parsing, and dedup stages are all working end to end on live data; flipping to live mode would create the {newc:,} net-new candidates above automatically, each with its résumé attached.
      </div>
    </div>"""


def send_impact_report(stats: dict, djc: dict | None = None,
                       recipients: Optional[Sequence[str]] = None) -> bool:
    """
    One-time, all-time impact report for the client — the full record of what the
    Kimedics → Salesforce automation has done since launch. Every figure is taken
    directly from the sync logs (volume, capture, sync time, hiring velocity,
    footprint); the only modeled number is hours saved, which is stated with its
    assumptions. No period-over-period deltas — this is the whole history.

    ``djc`` carries the Dentist Job Cafe pipeline's real dry-run scrape KPIs (from
    its separate DB); when provided, the DJC section renders those instead of a
    placeholder.
    """
    cur    = stats.get("current") or {}
    period = stats.get("period_label", "Since launch")

    emails  = int(cur.get("emails_received", 0))
    opened  = int(cur.get("opened", 0))
    updated = int(cur.get("updated", 0))
    closed  = int(cur.get("closed", 0))
    patches = int(cur.get("field_patches_total", 0))
    latency = cur.get("mean_latency_min")

    cap_ok   = int(cur.get("content_ok", 0))
    cap_tot  = int(cur.get("content_emails", 0))
    cap_pass = emails - max(0, cap_tot - cap_ok)
    cov      = (cap_pass / emails * 100) if emails else 100.0
    cov_str  = f"{cov:.1f}%"

    hrs_saved     = float(stats.get("hours_saved_estimate", 0.0))
    top_states    = stats.get("top_states", [])
    states_total  = int(stats.get("states_total", 0))
    monthly_trend = stats.get("monthly_trend", [])
    hours_trend   = stats.get("hours_trend", [])
    lifecycle     = stats.get("lifecycle", {}) or {}
    lc_all        = lifecycle.get("all", {}) or {}
    model         = stats.get("manual_time_model", {}) or {}
    launch        = stats.get("launch_iso") or ""
    today         = stats.get("today_iso") or ""
    days_live     = int(stats.get("days_live", 0))

    if emails == 0:
        print("[alert_email] Impact report skipped — no data")
        return False

    launch_disp = ""
    if launch:
        from datetime import date
        try:
            launch_disp = date.fromisoformat(launch).strftime("%B %-d, %Y")
        except ValueError:
            launch_disp = launch

    lat_txt = f"{latency:.1f} min" if latency is not None else "—"
    subject = f"Proxi · Kimedics → Salesforce Impact Report · {hrs_saved:g} hrs saved, {emails:,} emails"

    kimedics_header = _pulse_section_header("Kimedics → Salesforce", "Live", "live")

    # ── ROI centerpiece — the headline: hours of manual work returned ────────
    # All-time recouped line chart intentionally omitted — the per-month trend on the
    # whole-history report blurred with the single all-time total and added noise. The
    # weekly/monthly reports still show the trend; here we show just the headline number.
    spark = ""
    spark_cell = (f'<td class="spark-cell" style="vertical-align:bottom;width:38%;padding-left:20px;">{spark}</td>'
                  if spark else "")
    roi_html = f"""
    <div class="roi" style="margin:18px 0 0;padding:24px 26px;background:#18181b;border-radius:8px;">
      <table role="presentation" cellpadding="0" cellspacing="0" class="duo-stack" style="width:100%;border-collapse:collapse;">
        <tr>
          <td style="vertical-align:top;">
            <div style="color:#a1a1aa;font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;">Manual time recouped since launch</div>
            <div style="margin-top:10px;white-space:nowrap;">
              <span style="color:#ffffff;font-size:52px;font-weight:700;line-height:1.0;letter-spacing:-0.02em;">{hrs_saved:g} hrs</span>
            </div>
            <div style="color:#71717a;font-size:12px;margin-top:14px;line-height:1.5;">Estimated hands-on data-entry time the automation handled across {days_live} days.</div>
          </td>
          {spark_cell}
        </tr>
      </table>
    </div>"""

    # ── What the automation processed — the measured volume ──────────────────
    # Hiring-velocity metrics that replace the misleading opened/closed event counts:
    # median time a job stays open, and the count of jobs that completed a full open→close
    # lifecycle (distinct jobs, not status emails).
    med_open    = _pulse_fmt_dur(lc_all.get("median_hours"))
    jobs_filled = int(lc_all.get("closed_total", 0))
    hero_big = f"""
    <table role="presentation" cellpadding="0" cellspacing="8" class="hero-stack" style="width:100%;border-collapse:separate;margin-top:10px;">
      <tr>
        {_pulse_card_big("Emails processed",     f"{emails:,}",  _pulse_sub("every Kimedics job email"))}
        {_pulse_card_big("Salesforce updates",   f"{patches:,}", _pulse_sub("fields written automatically"))}
        {_pulse_card_big("Jobs filled",          f"{jobs_filled:,}", _pulse_sub("completed open → close"))}
      </tr>
    </table>"""
    hero_a = f"""
    <table role="presentation" cellpadding="0" cellspacing="8" class="hero-stack" style="width:100%;border-collapse:separate;margin-top:8px;">
      <tr>
        {_pulse_card_sm("Median time open", med_open,    _pulse_sub("typical open → filled"))}
        {_pulse_card_sm("Jobs updated",     f"{updated:,}", _pulse_sub("change syncs pushed"))}
        {_pulse_card_sm("Capture rate",     cov_str,        _pulse_sub(f"{cap_pass:,} of {emails:,} captured"))}
      </tr>
    </table>"""
    hero_b = f"""
    <table role="presentation" cellpadding="0" cellspacing="8" class="hero-stack" style="width:100%;border-collapse:separate;margin-top:8px;">
      <tr>
        {_pulse_card_sm("Avg sync time",  lat_txt,             _pulse_sub("email → Salesforce"))}
        {_pulse_card_sm("States active",  f"{states_total}",   _pulse_sub("national coverage"))}
        {_pulse_card_sm("Days live",      f"{days_live}",      _pulse_sub(f"since {launch_disp or launch}"))}
      </tr>
    </table>"""
    hero_row = hero_big + hero_a + hero_b

    # ── Growth trajectory — activity by month ────────────────────────────────
    monthly_trend_html = ""
    if monthly_trend:
        partial = ""
        if today:
            from datetime import date
            try:
                d = date.fromisoformat(today)
                if d.day < 28:
                    partial = (f'<div class="dim" style="font-size:11px;color:#a1a1aa;margin-top:12px;line-height:1.5;">'
                               f'{monthly_trend[-1][0]} reflects activity through {d.strftime("%b %-d")} (month-to-date).</div>')
            except ValueError:
                pass
        monthly_trend_html = f"""
        <div class="card" style="margin:18px 0 0;padding:20px 22px 16px;background:#ffffff;border:1px solid #e4e4e7;border-radius:8px;">
          <div class="dim" style="font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.06em;margin-bottom:16px;">Activity by month · emails handled</div>
          {_pulse_hist(list(monthly_trend), bar_h=92)}
          {partial}
        </div>"""

    # ── Job flow — opened vs closed across the whole history ──────────────────
    # Open/closed flow card removed — event counts, not job state (see send_weekly_summary).
    flow_html = ""

    # ── Hiring velocity — how long jobs stayed open (all-time) ────────────────
    lifecycle_html = ""
    if lc_all.get("closed_total"):
        alltot = int(lc_all.get("closed_total", 0)) or 1
        buckets = [
            ("Within 1 hr", int(lc_all.get("lt_1h", 0)),  "seg-close"),
            ("1–24 hrs",    int(lc_all.get("h1_24", 0)),  "seg-upd"),
            ("1–7 days",    int(lc_all.get("d1_7", 0)),   "seg-open"),
            ("Over 7 days", int(lc_all.get("gt_7d", 0)),  "bar-track"),
        ]
        bar = _pulse_seg_bar([(n, cls) for _l, n, cls in buckets], alltot, 14)
        rows = "".join(
            f'<tr>'
            f'<td style="padding:5px 0;font-size:12px;white-space:nowrap;width:50%;">'
            f'<span class="{cls}" style="display:inline-block;width:9px;height:9px;border-radius:2px;vertical-align:middle;margin-right:6px;"></span>'
            f'<span class="text" style="color:#27272a;">{lbl}</span></td>'
            f'<td class="muted" style="padding:5px 0;font-size:12px;color:#52525b;text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums;">{n:,} &nbsp;·&nbsp; {round(n / alltot * 100)}%</td>'
            f'</tr>'
            for lbl, n, cls in buckets
        )
        lifecycle_html = f"""
        <div class="card" style="margin:18px 0 0;padding:20px 22px;background:#ffffff;border:1px solid #e4e4e7;border-radius:8px;">
          <div class="dim" style="font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.06em;">Hiring velocity · how long jobs stayed open</div>
          <div class="num" style="font-size:24px;color:#18181b;font-weight:700;margin:6px 0 2px;letter-spacing:-0.02em;">{_pulse_fmt_dur(lc_all.get("median_hours"))} median open</div>
          <div class="muted" style="font-size:13px;color:#52525b;margin-bottom:14px;">across {alltot:,} jobs with a completed open → close lifecycle</div>
          {bar}
          <table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;margin-top:14px;">{rows}</table>
        </div>"""

    map_html = _pulse_us_map(top_states, states_total, "Every job by state since launch.")

    body_inner = (kimedics_header + roi_html + hero_row + monthly_trend_html
                  + flow_html + lifecycle_html + map_html)
    body_html = _pulse_doc("Proxi · Kimedics impact report", period, body_inner,
                           "Proxi · Kimedics Impact Report")

    text = textwrap.dedent(f"""
    Proxi — Kimedics → Salesforce Impact Report
    {period}
    ============================================
    Live since launch ({launch_disp or launch}), {days_live} days live.

    Manual time recouped : {hrs_saved:g} hrs (estimated)
    Emails processed     : {emails}
    Jobs filled          : {int(lc_all.get("closed_total", 0))} (completed open -> close)
    Median time open     : {_pulse_fmt_dur(lc_all.get("median_hours"))}
    Jobs updated         : {updated}
    Salesforce updates   : {patches} fields
    Capture rate         : {cov_str} ({cap_pass} of {emails} captured)
    Avg sync time        : {lat_txt} (email -> Salesforce)
    States active        : {states_total}

    Proxi · Kimedics -> Salesforce automation
    """).strip()

    return _send(subject, body_html, text, recipients=recipients)


# ── Low-confidence date resolution review alert ──────────────────────────────────────

def send_dates_review_alert(
    job_id: str,
    *,
    kind: str = "review",
    kimedics_url: str = "",
    sf_job_id: str = "",
    structured: str = "",
    resolved: str = "",
    confidence: int = 0,
    reason: str = "",
    top_text: str = "",
    recipients: Optional[Sequence[str]] = None,
) -> bool:
    """
    Date alert to ALERT_RECIPIENTS. ``kind='review'``: AI resolved dates with < 100%
    confidence — verify. ``kind='missing'``: no coverage dates could be captured at
    all — dates must always be present, so add them manually. ``top_text`` is the
    exact top-of-post text the resolver read, shown so reviewers can diagnose the call.
    """
    missing = kind == "missing"
    sf_url = (
        f"https://proxi.lightning.force.com/lightning/r/Job__c/{sf_job_id}/view"
        if sf_job_id else ""
    )
    rows = [("Job", f"#{job_id}")]
    if not missing:
        rows.append(("Confidence", f"{confidence}%"))
    rows += [
        ("Note" if missing else "AI reason", reason or "—"),
        ("Dates in post", structured or "—"),
        ("Captured dates", resolved or "(none)"),
    ]
    row_html = "".join(
        f'<tr><td style="padding:6px 14px 6px 0;color:#6b7280;font-size:13px;white-space:nowrap;vertical-align:top;">{label}</td>'
        f'<td style="padding:6px 0;color:#111827;font-size:13px;font-weight:600;">{value}</td></tr>'
        for label, value in rows
    )

    # The exact top-of-post text the resolver read — so a reviewer can see WHY the
    # AI decided what it did, not just the parsed result. Escaped; preserves line breaks.
    top_label = "Top of post — read by the AI" if not missing else "Top of post"
    top_block = (
        f'<div style="margin-top:16px;">'
        f'<div style="font-size:11px;font-weight:600;color:#6b7280;margin-bottom:5px;">{top_label}</div>'
        f'<div style="white-space:pre-wrap;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;'
        f'font-size:12px;line-height:1.55;color:#374151;background:#f9fafb;border:1px solid #e5e7eb;'
        f'border-radius:6px;padding:10px 12px;">{_html_escape(top_text)}</div></div>'
    ) if (top_text or "").strip() else ""
    links = []
    if kimedics_url:
        links.append(f'<a href="{kimedics_url}" style="color:#2563eb;">Kimedics post</a>')
    if sf_url:
        links.append(f'<a href="{sf_url}" style="color:#2563eb;">Salesforce job</a>')
    links_html = (" &nbsp;·&nbsp; ".join(links)) if links else ""

    if missing:
        border, kicker_color = "#dc2626", "#b91c1c"
        subject = f"⚠️ Proxi · No dates captured · Job #{job_id} — please add manually"
        kicker = "Coverage dates · MISSING"
        headline = f"Job #{job_id} — no coverage dates captured"
        lede = ("The automation could not capture any coverage dates from this post (it may use a "
                "non-standard format or omit a Dates line). Coverage dates must always be present — "
                "please review the post and add them in Salesforce.")
    else:
        border, kicker_color = "#fcd34d", "#b45309"
        subject = f"⚠️ Proxi · Date review needed · Job #{job_id} ({confidence}% confidence)"
        kicker = "Date resolution · review needed"
        headline = f"Job #{job_id} — active dates resolved at {confidence}% confidence"
        lede = ("The automation interpreted a top-line date update (an active-need / dates-added "
                "override, or a cancellation) but was not fully certain. Please verify the dates below.")

    html = f"""<!DOCTYPE html><html><body style="margin:0;padding:24px;background:#f9fafb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
      <div style="max-width:560px;margin:0 auto;background:#ffffff;border:1px solid {border};border-radius:10px;padding:22px 24px;">
        <div style="font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:{kicker_color};">{kicker}</div>
        <div style="font-size:17px;font-weight:700;color:#111827;margin:6px 0 4px;">{headline}</div>
        <div style="font-size:13px;color:#6b7280;line-height:1.6;margin-bottom:14px;">{lede}</div>
        <table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;">{row_html}</table>
        {top_block}
        {f'<div style="margin-top:16px;font-size:13px;">{links_html}</div>' if links_html else ''}
      </div>
      <div style="max-width:560px;margin:12px auto 0;font-size:11px;color:#9ca3af;text-align:center;">Proxi · Kimedics → Salesforce automation</div>
    </body></html>"""
    text = (
        f"{headline}\n{lede}\n\n"
        f"{'Note' if missing else 'AI reason'}: {reason or '—'}\n"
        f"Dates in post: {structured or '—'}\n"
        f"Captured dates: {resolved or '(none)'}\n"
        + (f"\n{top_label}:\n{top_text.strip()}\n" if (top_text or '').strip() else "")
        + (f"Kimedics: {kimedics_url}\n" if kimedics_url else "")
        + (f"Salesforce: {sf_url}\n" if sf_url else "")
    )
    return _send(subject, html, text, recipients=recipients)


# ── Practice-blocked (genuinely stuck) alert ─────────────────────────────────────────

def send_practice_block_alert(
    job_id: str,
    *,
    kimedics_url: str = "",
    sf_job_id: str = "",
    city: str = "",
    state: str = "",
    attempts: int = 0,
    recipients: Optional[Sequence[str]] = None,
) -> bool:
    """
    Layer-3 backstop: a job that could not capture ``practice_value`` and has exhausted
    automatic re-scrapes is genuinely stuck — it can't be created in Salesforce. Surface
    it immediately so a human can fix the post / link the practice, instead of leaving it
    silent. Fired once per job (deduped by the caller).
    """
    sf_url = (
        f"https://proxi.lightning.force.com/lightning/r/Job__c/{sf_job_id}/view"
        if sf_job_id else ""
    )
    loc = ", ".join([p for p in (city, state) if (p or "").strip()]) or "—"
    rows = [
        ("Job", f"#{job_id}"),
        ("Location", loc),
        ("Auto-recovery", f"{attempts} re-scrapes attempted — all missing the practice"),
    ]
    row_html = "".join(
        f'<tr><td style="padding:6px 14px 6px 0;color:#6b7280;font-size:13px;white-space:nowrap;vertical-align:top;">{label}</td>'
        f'<td style="padding:6px 0;color:#111827;font-size:13px;font-weight:600;">{value}</td></tr>'
        for label, value in rows
    )
    links = []
    if kimedics_url:
        links.append(f'<a href="{kimedics_url}" style="color:#2563eb;">Kimedics post</a>')
    if sf_url:
        links.append(f'<a href="{sf_url}" style="color:#2563eb;">Salesforce job</a>')
    links_html = (" &nbsp;·&nbsp; ".join(links)) if links else ""

    subject = f"⚠️ Proxi · Job stuck — practice not captured · Job #{job_id}"
    kicker = "Salesforce sync · BLOCKED"
    headline = f"Job #{job_id} — can't create in Salesforce (no practice value)"
    lede = ("The automation couldn't read this job's practice (the office-id line, e.g. "
            "\"4403 - Dublin, GA\") and automatic re-scrapes didn't recover it — so the job "
            "is held back to avoid creating a duplicate. Please open the Kimedics post and "
            "check the Practice field (an unlinked practice can render in a way the scrape "
            "misses); a manual rescrape usually clears it once the field reads.")
    html = f"""<!DOCTYPE html><html><body style="margin:0;padding:24px;background:#f9fafb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
      <div style="max-width:560px;margin:0 auto;background:#ffffff;border:1px solid #dc2626;border-radius:10px;padding:22px 24px;">
        <div style="font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#b91c1c;">{kicker}</div>
        <div style="font-size:17px;font-weight:700;color:#111827;margin:6px 0 4px;">{headline}</div>
        <div style="font-size:13px;color:#6b7280;line-height:1.6;margin-bottom:14px;">{lede}</div>
        <table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;">{row_html}</table>
        {f'<div style="margin-top:16px;font-size:13px;">{links_html}</div>' if links_html else ''}
      </div>
      <div style="max-width:560px;margin:12px auto 0;font-size:11px;color:#9ca3af;text-align:center;">Proxi · Kimedics → Salesforce automation</div>
    </body></html>"""
    text = (
        f"{headline}\n{lede}\n\n"
        f"Location: {loc}\n"
        f"Auto-recovery: {attempts} re-scrapes attempted — all missing the practice\n"
        + (f"Kimedics: {kimedics_url}\n" if kimedics_url else "")
        + (f"Salesforce: {sf_url}\n" if sf_url else "")
    )
    return _send(subject, html, text, recipients=recipients)


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
