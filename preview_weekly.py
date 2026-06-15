"""Render the weekly pulse locally against live DB and capture HTML to /tmp."""
import os, sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "production"))

for envf in (ROOT / ".env", ROOT / ".env.local"):
    if envf.exists():
        for line in envf.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'").strip('"'))

import utils.alert_email as ae  # type: ignore
captured = {}
def fake_send(subject, html, text=None, recipients=None):  # type: ignore
    captured["subject"] = subject
    captured["html"]    = html
    captured["text"]    = text
    return True
ae._send = fake_send

from utils.supabase_db import get_conn  # type: ignore
import scrape_gmail_modal as mod  # type: ignore
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")

# Patch datetime.datetime.now to simulate "today = tomorrow of busiest week" so
# the "previous calendar week" computation lines up with a real data range.
# We pick the most recent Monday that has any data going back to the prior week.
import datetime as _dt
real = _dt.datetime
target = datetime(2026, 6, 11, 12, 0, tzinfo=ET)  # Thu Jun 11 → reports on prior week Jun 1-7
class P(real):
    @classmethod
    def now(cls, tz=None):
        return target if tz is None else target.astimezone(tz)
_dt.datetime = P  # type: ignore

stats = mod._build_weekly_stats(get_conn)
_dt.datetime = real  # type: ignore

print("[preview] period:", stats["period_label"])
print("[preview] current:", {k: v for k, v in stats["current"].items() if not isinstance(v, (list, dict))})
print("[preview] daily split:", stats["daily_split_series"])
print("[preview] lifecycle:", stats["lifecycle"])
print("[preview] hours saved:", stats["hours_saved_estimate"], "(all-time:", stats["cumulative"]["hours_saved"], ")")

ok = ae.send_weekly_summary(stats)
if not ok:
    print("send_weekly_summary returned False"); sys.exit(2)
Path("/tmp/weekly_preview.html").write_text(captured["html"])
Path("/tmp/weekly_preview.txt").write_text(captured["text"] or "")
print(f"\nSubject: {captured['subject']}")
print(f"HTML: /tmp/weekly_preview.html ({len(captured['html'])} bytes)")
print(f"Text: /tmp/weekly_preview.txt ({len(captured['text'] or '')} bytes)")
