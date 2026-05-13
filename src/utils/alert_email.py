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

ALERT_RECIPIENTS = ["anddy0622@gmail.com", "seanhyang1@gmail.com"]
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
) -> bool:
    """
    Send an immediate alert email when a scraped job fails validation.

    Parameters
    ----------
    job_post_id   : Kimedics job post ID (e.g. "19476")
    issues        : list of ValidationIssue from validate_scraped_job()
    cleaned       : the parsed job dict (for the data snapshot section)
    view_job_link : direct link to the Kimedics job page
    """
    from utils.scrape_validator import Severity, issues_as_html, issues_summary

    summary = issues_summary(issues)
    critical_n = summary["critical"]
    warning_n  = summary["warning"]
    cleaned    = cleaned or {}

    # Subject line: escalate based on severity
    if critical_n > 0:
        subject = f"🚨 CRITICAL Scrape Alert — Job #{job_post_id} — {critical_n} critical issue(s)"
    else:
        subject = f"⚠️  Scrape Warning — Job #{job_post_id} — {warning_n} warnings"

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

    body = f"""
    <div class="section">
      <h2>Summary</h2>
      <p style="margin:0 0 8px;">Job <strong>#{job_post_id}</strong> failed validation with: {badges}</p>
      {link_html}
    </div>

    <div class="section">
      <h2>Validation Issues</h2>
      {issues_as_html(issues)}
    </div>

    <div class="section">
      <h2>Scraped Data Snapshot</h2>
      <table>{''.join(snap_rows)}</table>
    </div>

    <div class="section">
      <p style="font-size:12px;color:#888;margin:0;">
        This alert was sent because the scraped data does not meet quality thresholds.
        Check the Kimedics portal to verify the page is intact, then review
        <code>playwright_job_scrape.py</code> selectors if the issue persists.
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
    patches_total   = g("field_patches_total",  0)
    ext_id_swaps    = g("ext_id_swaps",         0)
    manual_rescr    = g("manual_rescrapes",     0)
    auto_retries    = g("auto_retries",         0)
    stuck_jobs      = g("stuck_jobs",           0)
    scrape_fails    = g("scrape_failures",      0)
    rows            = g("rows",                 [])

    if emails == 0:
        print(
            "[alert_email] Daily summary skipped — no emails received in the "
            f"reporting period ({period})"
        )
        return False

    # ── Health badge ──────────────────────────────────────────────────────────
    if stuck_jobs > 0 or scrape_fails > 0:
        health_badge = '<span class="badge badge-crit">NEEDS ATTENTION</span>'
        subject_pfx  = "🚨"
    elif ext_id_swaps > 0:
        health_badge = '<span class="badge badge-warn">REVIEW SWAPS</span>'
        subject_pfx  = "⚠️"
    else:
        health_badge = '<span class="badge badge-ok">HEALTHY</span>'
        subject_pfx  = "✅"

    subject = (
        f"{subject_pfx} Proxi Daily — {period} — "
        f"{emails} emails · {scraped_ok} scraped · {sf_mapped} mapped · {patches_total} field patches"
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
        + _box(sf_jobs_created, "New SF Records")
        + _box(patches_total,   "SF Fields Patched")
        + _box(ext_id_swaps,    "ID Swaps",         color_amber if ext_id_swaps else "#aaa")
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

            # SF mapping
            if r["sf_mapped"]:
                sf_html = (
                    _chip("✓", "green", f"sf_job_id={sfid}")
                    + (f'<span style="font-family:monospace;color:#666;font-size:10px;">{sfid[:8]}…</span>'
                       if sfid else "")
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

            # Notes
            notes: list[str] = []
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
    New SF records         : {sf_jobs_created}
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
