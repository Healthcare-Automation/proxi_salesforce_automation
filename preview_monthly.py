"""Render the monthly pulse locally against live DB and capture HTML to /tmp."""
import os, sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "production"))

for envf in (ROOT / ".env", ROOT / ".env.local"):
    if envf.exists():
        for line in envf.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
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
from datetime import datetime
from zoneinfo import ZoneInfo
import datetime as _dt

ET = ZoneInfo("America/New_York")
target = datetime(2026, 6, 13, 12, 0, tzinfo=ET)  # prior month = May 2026
real = _dt.datetime
class P(real):
    @classmethod
    def now(cls, tz=None):
        return target if tz is None else target.astimezone(tz)
_dt.datetime = P  # type: ignore
stats = mod._build_monthly_stats(get_conn)
_dt.datetime = real  # type: ignore

cur = stats["current"]
print("[preview] period:", stats["period_label"])
print("[preview] emails/opened/closed:", cur["emails_received"], cur["opened"], cur["closed"])
print("[preview] trend:", stats["trend_weekly"])

ok = ae.send_monthly_summary(stats)
if not ok:
    print("send_monthly_summary returned False"); sys.exit(2)
Path("/tmp/monthly_preview.html").write_text(captured["html"])
print(f"\nSubject: {captured['subject']}")
print(f"HTML: /tmp/monthly_preview.html ({len(captured['html'])} bytes)")
