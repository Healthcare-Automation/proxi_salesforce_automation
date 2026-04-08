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
    Send the daily digest email.

    Expected keys in stats (all optional / gracefully handled if missing):
      period_label        str
      emails_received     int
      scrape_attempts     int
      scrape_success      int   — all critical fields present, no major issues
      scrape_partial      int   — title_line present but 3+ warnings
      scrape_failed       int   — title_line empty (complete failure)
      total_warnings      int
      total_criticals     int
      sf_mapped           int   — jobs with sf_job_id resolved
      sf_unmapped         int   — successfully scraped but no sf_job_id yet
      sf_worksite_mapped  int   — jobs with sf_worksite_account_id resolved
      sf_unmapped_jobs    list[dict]  — {job_post_id, job_title, status, posting_org,
                                         view_job_link, scraped_at}
      example_jobs        list[dict]  — up to 5 recent successful job dicts
      issue_log           list[dict]  — {job_post_id, issues_text} for flagged jobs
      runs                list[dict]  — {run_id, run_type, started_at, finished_at}
    """
    g = stats.get

    period           = g("period_label",       "Last 24 hours")
    emails           = g("emails_received",     0)
    attempts         = g("scrape_attempts",     0)
    success          = g("scrape_success",      0)
    partial          = g("scrape_partial",      0)
    failed           = g("scrape_failed",       0)
    warnings         = g("total_warnings",      0)
    criticals        = g("total_criticals",     0)
    sf_mapped        = g("sf_mapped",           0)
    sf_unmapped      = g("sf_unmapped",         0)
    sf_ws_mapped     = g("sf_worksite_mapped",  0)
    sf_unmapped_jobs = g("sf_unmapped_jobs",    [])
    examples         = g("example_jobs",        [])
    issue_log        = g("issue_log",           [])
    runs             = g("runs",                [])

    # Overall health badge
    sf_map_rate = round(sf_mapped / success * 100) if success else 0
    if criticals > 0 or failed > 0:
        health_badge = '<span class="badge badge-crit">NEEDS ATTENTION</span>'
        subject_pfx  = "🚨"
    elif warnings > 3 or partial > 0 or sf_unmapped > 0:
        health_badge = '<span class="badge badge-warn">WARNINGS</span>'
        subject_pfx  = "⚠️"
    else:
        health_badge = '<span class="badge badge-ok">HEALTHY</span>'
        subject_pfx  = "✅"

    subject = (
        f"{subject_pfx} Proxi Daily Report — {period} — "
        f"{success}/{attempts} scraped · {sf_mapped}/{success} SF mapped"
    )

    # ── Stat boxes ────────────────────────────────────────────────────────────
    def _box(num, label, color="#1a1a2e"):
        return (
            f'<div class="stat-box">'
            f'<div class="num" style="color:{color};">{num}</div>'
            f'<div class="lbl">{label}</div></div>'
        )

    stats_html = (
        '<div class="stat-row">'
        + _box(emails,      "Emails Received")
        + _box(attempts,    "Scrape Attempts")
        + _box(success,     "Scrapes OK",         "#27ae60")
        + _box(partial,     "Partial",             "#d35400" if partial else "#aaa")
        + _box(failed,      "Failed",              "#c0392b" if failed  else "#aaa")
        + _box(sf_mapped,   "SF Job ID Mapped",    "#27ae60" if sf_unmapped == 0 else "#d35400")
        + _box(sf_unmapped, "SF Not Mapped Yet",   "#c0392b" if sf_unmapped  > 0 else "#aaa")
        + _box(sf_ws_mapped,"SF Worksite Mapped")
        + "</div>"
    )

    # ── Pipeline runs ─────────────────────────────────────────────────────────
    runs_rows = ""
    if runs:
        for r in runs[:15]:
            g_id  = r.get("gmail_run_id")
            lb_id = r.get("link_batch_run_id")
            # Run IDs: show both if paired
            if g_id and lb_id:
                ids = f'<span style="color:#555;">#{g_id}</span> + <span style="color:#555;">#{lb_id}</span>'
            else:
                ids = f'<span style="color:#555;">#{g_id or lb_id}</span>'
            start    = str(r.get("started_at",  ""))[:19]
            finished = str(r.get("finished_at", ""))[:19] or "<em>running…</em>"
            dur      = r.get("duration", "—")
            g_dur    = r.get("gmail_dur", "—")
            lb_dur   = r.get("link_batch_dur", "—")
            breakdown = f'<span style="font-size:11px;color:#999;">gmail {g_dur} · scrape {lb_dur}</span>'
            runs_rows += (
                f"<tr>"
                f"<td class='mono'>{ids}</td>"
                f"<td class='mono'>{start}</td>"
                f"<td class='mono'>{finished}</td>"
                f"<td style='font-weight:600;'>{dur}</td>"
                f"<td>{breakdown}</td>"
                f"</tr>"
            )
        runs_section = f"""
        <div class="section">
          <h2>Pipeline Runs</h2>
          <table style="font-size:12px;">
            <tr>
              <th>Run IDs</th>
              <th>Started</th>
              <th>Finished</th>
              <th>Duration</th>
              <th>Breakdown</th>
            </tr>
            {runs_rows}
          </table>
        </div>"""
    else:
        runs_section = ""

    # ── SF mapping section ────────────────────────────────────────────────────
    if sf_unmapped_jobs:
        unmap_rows = ""
        for j in sf_unmapped_jobs[:30]:
            jid   = j.get("job_post_id", "?")
            jtit  = j.get("job_title", "—")
            jstat = j.get("status", "—")
            jorg  = j.get("posting_org", "—")
            jtime = j.get("scraped_at", "")[:16]
            link  = j.get("view_job_link", "")
            jlink = f'<a href="{link}" style="color:#2471a3;">#{jid}</a>' if link else f"#{jid}"
            unmap_rows += (
                f"<tr><td>{jlink}</td><td>{jtit}</td>"
                f"<td>{jstat}</td><td>{jorg}</td><td class='mono'>{jtime}</td></tr>"
            )
        sf_section = f"""
        <div class="section">
          <h2>Salesforce Mapping Status</h2>
          <p style="margin:0 0 10px;">
            <strong style="color:#27ae60;">{sf_mapped}</strong> jobs have SF Job ID &nbsp;·&nbsp;
            <strong style="color:#d35400;">{sf_unmapped}</strong> successfully scraped but not yet mapped
            &nbsp;·&nbsp; <strong>{sf_ws_mapped}</strong> have worksite resolved
            &nbsp;·&nbsp; Mapping rate: <strong>{sf_map_rate}%</strong>
          </p>
          <p style="font-size:12px;color:#888;margin:0 0 10px;">
            Unmapped jobs below were scraped successfully but <code>sf_job_id</code> has not been
            resolved yet. This usually means the SF record doesn't exist yet or the resolve step
            is pending. Not necessarily an error — check if these are new jobs.
          </p>
          <table style="font-size:12px;">
            <tr><th>Job</th><th>Title</th><th>Status</th><th>Org</th><th>Scraped At</th></tr>
            {unmap_rows}
          </table>
        </div>"""
    else:
        sf_icon = "✓" if sf_mapped > 0 else "—"
        sf_section = f"""
        <div class="section">
          <h2>Salesforce Mapping Status</h2>
          <p style="color:#27ae60;">{sf_icon} All {sf_mapped} scraped job(s) have SF Job ID mapped.
          Worksite resolved: {sf_ws_mapped}. Mapping rate: {sf_map_rate}%.</p>
        </div>"""

    # ── Example jobs ──────────────────────────────────────────────────────────
    example_fields = [
        ("job_id",              "Job ID"),
        ("job_title",           "Title"),
        ("status",              "Status"),
        ("city",                "City"),
        ("state",               "ST"),
        ("provider_start_date", "Start"),
        ("posting_org",         "Org"),
        ("sf_job_id",           "SF Job ID"),
    ]
    if examples:
        col_headers = "".join(f"<th>{lbl}</th>" for _, lbl in example_fields)
        ex_rows = ""
        for ex in examples:
            cells = ""
            for key, _ in example_fields:
                val = (ex.get(key) or "").strip()
                if not val and key == "sf_job_id":
                    cells += '<td style="color:#d35400;font-style:italic;">not mapped</td>'
                else:
                    cells += f'<td>{val or "—"}</td>'
            ex_rows += f"<tr>{cells}</tr>"
        examples_section = f"""
        <div class="section">
          <h2>All Scraped Jobs ({len(examples)})</h2>
          <p style="font-size:12px;color:#888;margin:0 0 10px;">
            All successfully scraped jobs this period. SF Job ID column confirms end-to-end pipeline health.</p>
          <table style="font-size:12px;">
            <tr>{col_headers}</tr>
            {ex_rows}
          </table>
        </div>"""
    else:
        examples_section = (
            '<div class="section"><h2>All Scraped Jobs</h2>'
            '<p style="color:#c0392b;">No successful scrapes in this period.</p></div>'
        )

    # ── Flagged jobs ──────────────────────────────────────────────────────────
    if issue_log:
        flag_rows = ""
        for entry in issue_log[:20]:
            jid   = entry.get("job_post_id", "?")
            link  = entry.get("view_job_link", "")
            itxt  = entry.get("issues_text", "").replace("\n", "<br>")
            jlink = f'<a href="{link}">#{jid}</a>' if link else f"#{jid}"
            flag_rows += f"<tr><td style='white-space:nowrap;'>{jlink}</td><td>{itxt}</td></tr>"
        flagged_section = f"""
        <div class="section">
          <h2>Flagged Jobs ({len(issue_log)} issues)</h2>
          <table>
            <tr><th>Job</th><th>Issues</th></tr>
            {flag_rows}
          </table>
        </div>"""
    else:
        flagged_section = (
            '<div class="section"><h2>Flagged Jobs</h2>'
            '<p style="color:#27ae60;">✓ No issues flagged in this period.</p></div>'
        )

    # ── Assemble body ─────────────────────────────────────────────────────────
    body = f"""
    <div class="section">
      <h2>Overall Health — {period}</h2>
      <p style="margin:0 0 12px;">Status: {health_badge}
        &nbsp;·&nbsp; {criticals} critical issue(s) · {warnings} warning(s)</p>
      {stats_html}
    </div>

    {sf_section}

    {runs_section}

    {examples_section}

    {flagged_section}

    <div class="section">
      <p style="font-size:12px;color:#888;margin:0;">
        This report covers activity in the last 24 hours.
        Critical alerts are sent immediately when they occur.
        Reply to this email or contact andy if something looks wrong.
      </p>
    </div>
    """

    # Plain-text fallback
    text = textwrap.dedent(f"""
    Proxi Daily Scrape Report — {period}
    {'='*50}
    Emails received   : {emails}
    Scrape attempts   : {attempts}
    Successful        : {success}
    Partial           : {partial}
    Failed            : {failed}
    Total warnings    : {warnings}
    Total criticals   : {criticals}

    SF Mapping
    ----------
    SF Job ID mapped  : {sf_mapped} / {success} ({sf_map_rate}%)
    Not yet mapped    : {sf_unmapped}
    Worksite mapped   : {sf_ws_mapped}

    Unmapped jobs:
    {chr(10).join(f'  #{j.get("job_post_id","?")} {j.get("job_title","?")} | {j.get("status","?")} | {j.get("posting_org","?")}' for j in sf_unmapped_jobs[:15]) or "  None"}

    Recent jobs (example data):
    {chr(10).join(f'  #{e.get("job_id","?")} {e.get("job_title","?")} | {e.get("status","?")} | SF:{e.get("sf_job_id","—")}' for e in examples[:5]) or "  None"}

    Flagged:
    {chr(10).join(f'  #{e.get("job_post_id","?")} — {e.get("issues_text","?")}' for e in issue_log[:10]) or "  None"}
    """).strip()

    return _send(
        subject,
        _html_wrap(
            "📊 Proxi Daily Scrape Report",
            f"{period} · Proxi Salesforce Automation",
            body,
        ),
        text,
    )
