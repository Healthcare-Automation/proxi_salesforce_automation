"""
Shared Playwright job-page scraper: one login, then visit each URL and extract body + description.
Used by tests/scrape_kimedics_batch_playwright.py and Modal. Returns list of {job_post_id, email_received_date, cleaned, error}.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

from utils.job_content_parser import repair_flat_jobpost_text_missing_posted_date
from utils.sf_job_payload import KIMEDICS_PORTAL_JOB_POST_URL_PREFIX

_utils = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Timeouts (ms)
# ---------------------------------------------------------------------------
WAIT_FOR_LOGIN_FORM_MS = 15_000
AFTER_LOGIN_WAIT_MS = 4_000
NAV_TIMEOUT_MS = 30_000
WAIT_AFTER_GOTO_MS = 3_000
WAIT_FOR_DESCRIPTION_MS = 12_000
BETWEEN_URLS_S = 0.5

# After navigation, wait for *either* the job content container or a login form.
# If neither appears within this time we still try to extract what we can.
WAIT_FOR_CONTENT_OR_LOGIN_MS = 20_000

# Maximum number of times we retry a single URL when we detect a login page.
# Increased to be more aggressive about getting logged in
MAX_LOGIN_RETRIES = 5

# Tokens that indicate the extracted text is a login/auth page, not job content.
_LOGIN_PAGE_MARKERS = frozenset({
    "auth0",
    "log in",
    "sign in",
    "sign-in",
    "forgot password",
    "use auth0",
    "enter your password",
    "don't have an account",
})

# Tokens that indicate we're on a logged-out state (seeing navigation/UI elements)
_LOGGED_OUT_MARKERS = frozenset({
    "sign out",
    "signout",
    "log out",
    "logout",
})


# ---------------------------------------------------------------------------
# Browser-side JS: recover Posted Date from the Kimedics two-column layout.
# ---------------------------------------------------------------------------
_KIMEDICS_POSTED_DATE_JS = r"""
(selector) => {
  const root = document.querySelector(selector);
  if (!root) return '';
  const DATE_RE = /\b(\d{1,2}\/\d{1,2}\/\d{2,4}|\d{4}-\d{2}-\d{2})\b/;
  const LABEL_BLOCKLIST = /^(Posting Org|Priority|Status|Job Title)$/i;

  const pickDateString = (s) => {
    const t = (s || '').trim();
    if (!t) return '';
    const m = t.match(DATE_RE);
    return m ? m[0] : '';
  };

  for (const inp of root.querySelectorAll('input, select')) {
    const val = (inp.value || '').trim();
    if (!val) continue;
    const hint = (inp.id || '') + (inp.name || '') + (inp.getAttribute('aria-label') || '')
      + (inp.getAttribute('placeholder') || '');
    if (inp.type === 'date' || /posted|post.?date/i.test(hint)) {
      if (DATE_RE.test(val) || val.length >= 6) return val;
    }
  }

  const leaves = Array.from(root.querySelectorAll('*')).filter(
    (el) => el.childElementCount === 0 && (el.textContent || '').trim() === 'Posted Date'
  );
  for (const leaf of leaves) {
    const parent = leaf.parentElement;
    if (!parent) continue;
    const sibs = Array.from(parent.children);
    const idx = sibs.indexOf(leaf);
    for (let j = idx + 1; j < sibs.length; j++) {
      const cell = sibs[j];
      const t = (cell.innerText || cell.textContent || '').trim();
      if (t && !LABEL_BLOCKLIST.test(t)) {
        const hit = pickDateString(t);
        if (hit) return hit;
      }
      const inp = cell.matches?.('input') ? cell : cell.querySelector?.('input, select');
      if (inp && inp.value) {
        const v = inp.value.trim();
        if (v && (DATE_RE.test(v) || inp.type === 'date')) return v;
      }
    }
  }

  function pairedColumns(node, depth) {
    if (!node || depth > 10) return '';
    const kids = Array.from(node.children);
    if (kids.length === 2) {
      const textLines = (el) =>
        (el.innerText || '')
          .split(/\n/)
          .map((s) => s.trim())
          .filter(Boolean);
      const a = textLines(kids[0]);
      const b = textLines(kids[1]);
      const i = a.indexOf('Posted Date');
      if (i >= 0 && i < b.length) {
        const hit = pickDateString(b[i]);
        if (hit) return hit;
      }
    }
    for (const c of kids) {
      const r = pairedColumns(c, depth + 1);
      if (r) return r;
    }
    return '';
  }
  return pairedColumns(root, 0);
}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_posted_date_from_kimedics_page(page) -> str:
    try:
        v = page.evaluate(_KIMEDICS_POSTED_DATE_JS, ".sections__container")
        return (v or "").strip() if isinstance(v, str) else ""
    except Exception:
        return ""


def augment_kimedics_job_body_text(page, body_text: str) -> str:
    """Splice DOM posted date into flat text before ``parse_job_content_txt``."""
    posted = extract_posted_date_from_kimedics_page(page)
    return repair_flat_jobpost_text_missing_posted_date(body_text, posted or None)


def canonical_kimedics_url(job_post_id: Optional[str]) -> Optional[str]:
    """Stable portal URL when the job_post_id is numeric (e.g. ``19531``)."""
    jid = (job_post_id or "").strip()
    if jid and jid.isdigit():
        return f"{KIMEDICS_PORTAL_JOB_POST_URL_PREFIX}{jid}"
    return None


def _looks_like_login_page(text: str) -> bool:
    """Return True when extracted text is clearly an auth/login page, not job content."""
    if not text or not text.strip():
        return False
    low = text.strip().lower()
    # Short pages dominated by login keywords
    if len(low) < 800:
        hits = sum(1 for m in _LOGIN_PAGE_MARKERS if m in low)
        if hits >= 2:
            return True
    # Any page where the first ~300 chars are just login UI
    head = low[:300]
    if sum(1 for m in _LOGIN_PAGE_MARKERS if m in head) >= 2:
        return True
    return False


def _is_logged_out_page(text: str) -> bool:
    """Return True when we detect 'Sign Out' or similar UI elements indicating we're not on job content."""
    if not text or not text.strip():
        return False

    low = text.strip().lower()

    # Check if page is suspiciously short and contains logout markers
    if len(low) < 1000:
        for marker in _LOGGED_OUT_MARKERS:
            if marker in low:
                # Found sign out but no job content markers = we're on a nav/UI page
                if not _looks_like_job_content(text):
                    return True

    # Check if "Sign Out" appears in first few lines (UI element position)
    lines = text.strip().split('\n')
    for line in lines[:10]:  # Check first 10 lines
        line_low = line.lower().strip()
        for marker in _LOGGED_OUT_MARKERS:
            if marker in line_low:
                return True

    return False


def _looks_like_job_content(text: str) -> bool:
    """Positive signal that the text is real Kimedics job content."""
    if not text or len(text.strip()) < 60:
        return False
    low = text.lower()

    # If we detect logged out markers, it's definitely not job content
    if _is_logged_out_page(text):
        return False

    # Need strong evidence this is actual job content
    required_markers = ("job title", "posted date", "posting org")
    optional_markers = ("practice", "status", "priority", "provider start date", "description")

    required_found = sum(1 for m in required_markers if m in low)
    optional_found = sum(1 for m in optional_markers if m in low)

    # Must have at least 2 required markers and 2 optional markers
    return required_found >= 2 and optional_found >= 2


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def ensure_kimedics_logged_in(
    page,
    email: str,
    password: str,
    *,
    form_timeout_ms: int = WAIT_FOR_LOGIN_FORM_MS,
) -> bool:
    """
    Submit credentials when the login form is visible. Returns True if login was submitted.
    Uses ``networkidle`` after click so redirects + SPA hydration complete before returning.
    """
    if not email or not password:
        return False
    try:
        loc = page.locator('input[type="email"]')
        if loc.count() == 0:
            return False
        loc.first.wait_for(state="visible", timeout=form_timeout_ms)
        page.fill('input[type="email"]', email)
        page.fill('input[type="password"]', password)
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
        page.wait_for_timeout(AFTER_LOGIN_WAIT_MS)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Core: navigate + wait + extract
# ---------------------------------------------------------------------------

def _wait_for_content_or_login(page, timeout_ms: int = WAIT_FOR_CONTENT_OR_LOGIN_MS) -> str:
    """
    After navigation, wait until we see either the job content container or a login form.
    Returns ``"content"``, ``"login"``, or ``"timeout"`` (neither appeared).
    """
    js = """
    (timeout) => new Promise((resolve) => {
        const deadline = Date.now() + timeout;
        const check = () => {
            const job = document.querySelector('.sections__container');
            if (job && job.offsetHeight > 0) return resolve('content');
            const login = document.querySelector('input[type="email"]');
            if (login && login.offsetHeight > 0) return resolve('login');
            if (Date.now() > deadline) return resolve('timeout');
            requestAnimationFrame(check);
        };
        check();
    })
    """
    try:
        return page.evaluate(js, timeout_ms) or "timeout"
    except Exception:
        return "timeout"


def _navigate_and_login_if_needed(
    page,
    url: str,
    email: str,
    password: str,
    *,
    final_url: Optional[str] = None,
) -> None:
    """
    Navigate to ``url`` using ``networkidle``, detect login vs job content, handle login
    if needed, and finally navigate to ``final_url`` (or ``url``) so the caller lands on
    the job page ready for extraction.

    ``final_url`` lets you login through a canonical URL but end up on a different page.
    """
    target = final_url or url
    page.goto(url, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
    page.wait_for_timeout(WAIT_AFTER_GOTO_MS)
    signal = _wait_for_content_or_login(page)

    if signal == "login":
        if ensure_kimedics_logged_in(page, email, password):
            page.goto(target, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
            page.wait_for_timeout(WAIT_AFTER_GOTO_MS)
            _wait_for_content_or_login(page)
        return

    if signal == "content":
        if target != url:
            page.goto(target, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
            page.wait_for_timeout(WAIT_AFTER_GOTO_MS)
        return

    # Timeout — might be a slow SPA. Give login form one more chance.
    if ensure_kimedics_logged_in(page, email, password, form_timeout_ms=5_000):
        page.goto(target, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
        page.wait_for_timeout(WAIT_AFTER_GOTO_MS)
        _wait_for_content_or_login(page)


def _extract_body_and_desc(page) -> tuple[str, str]:
    """Wait for the description textarea to hydrate, then pull body text + desc value."""
    try:
        page.wait_for_selector(".sections__container", state="visible", timeout=8_000)
        page.wait_for_function(
            "() => { const ta = document.querySelector('section-builder.full-jobpost textarea'); "
            "return ta && ta.value && ta.value.trim().length > 20; }",
            timeout=WAIT_FOR_DESCRIPTION_MS,
        )
    except Exception:
        pass
    page.wait_for_timeout(1_000)

    main_el = page.locator(".sections__container")
    if main_el.count() and main_el.first.is_visible():
        bt = main_el.first.inner_text()
    else:
        bt = page.locator("body").inner_text()
    bt = augment_kimedics_job_body_text(page, bt)

    dv = ""
    for sel in (
        "section-builder.full-jobpost textarea",
        "textarea[id*='fullJobPost']",
        "textarea[id*='full_job_post']",
    ):
        loc = page.locator(sel)
        if loc.count():
            dv = (loc.first.input_value() or "").strip()
            if dv:
                break
    return bt, dv


def visit_and_extract(
    page,
    url: str,
    job_post_id: str,
    email: str,
    password: str,
) -> tuple[str, str]:
    """
    Full robust flow: navigate → login if needed → extract → detect login page →
    retry with canonical URL if necessary.

    Returns ``(body_text, desc_value)`` ready for ``parse_job_content_txt``.
    """
    canon = canonical_kimedics_url(job_post_id)
    # Prefer the canonical URL — it avoids slow SendGrid redirect chains entirely.
    primary_url = canon or url

    for attempt in range(1, MAX_LOGIN_RETRIES + 2):
        _navigate_and_login_if_needed(page, primary_url, email, password)
        bt, dv = _extract_body_and_desc(page)

        # Check if we have valid job content
        if _looks_like_job_content(bt):
            return bt, dv

        # Detect various failure modes
        is_login_page = _looks_like_login_page(bt)
        is_logged_out = _is_logged_out_page(bt)
        is_empty = not bt.strip() and not dv

        if is_login_page or is_logged_out or is_empty:
            if attempt <= MAX_LOGIN_RETRIES:
                reason = "login page" if is_login_page else "logged out" if is_logged_out else "empty"
                print(
                    f"  [attempt {attempt}/{MAX_LOGIN_RETRIES}] page looks like {reason} — forcing re-authentication...",
                    file=sys.stderr,
                )

                # Clear cookies and force fresh login
                page.context.clear_cookies()

                # Force a login: go to the portal root which always shows the form.
                page.goto(
                    KIMEDICS_PORTAL_JOB_POST_URL_PREFIX.rstrip("/"),
                    wait_until="networkidle",
                    timeout=NAV_TIMEOUT_MS,
                )
                page.wait_for_timeout(WAIT_AFTER_GOTO_MS)

                # Try logging in
                login_success = ensure_kimedics_logged_in(page, email, password)
                if not login_success:
                    print(
                        f"  [attempt {attempt}] Login submission failed, waiting and retrying...",
                        file=sys.stderr,
                    )
                    page.wait_for_timeout(3000)  # Wait 3 seconds before retry

                continue

            # Last resort: try the alternate URL
            alt = url if primary_url == canon else canon
            if alt and alt != primary_url:
                print(
                    f"  [final attempt] trying alternate URL: {alt[:70]}...",
                    file=sys.stderr,
                )
                page.context.clear_cookies()  # Clear cookies for fresh attempt
                _navigate_and_login_if_needed(page, alt, email, password)
                bt, dv = _extract_body_and_desc(page)

                # Final check - if still not valid content, don't return it
                if _looks_like_job_content(bt):
                    return bt, dv

        # If we got some text but it's not recognized as valid, try once more
        elif bt.strip() and attempt == 1:
            print(
                f"  [attempt {attempt}] Got text but not valid job content, retrying...",
                file=sys.stderr,
            )
            continue
        else:
            # We have content but it's not matching our patterns - could be a new format
            # Log a warning but return it anyway (better than nothing)
            if bt.strip():
                print(
                    f"  WARNING: Returning content that doesn't match expected format for job {job_post_id}",
                    file=sys.stderr,
                )
            return bt, dv

    # All retries exhausted - this should trigger an alert
    print(
        f"  ERROR: Failed to get valid content for job {job_post_id} after {MAX_LOGIN_RETRIES} attempts!",
        file=sys.stderr,
    )
    return "", ""


# ---------------------------------------------------------------------------
# Public: batch scrape (used by Modal + run_incremental)
# ---------------------------------------------------------------------------

def scrape_job_pages(
    rows_with_email_scrape_id: list[tuple[dict, int]],
    kimedics_email: str,
    kimedics_password: str,
) -> list[dict]:
    """
    For each (row, email_scrape_id): visit row["view_job_link"], extract body + description textarea, parse.
    Returns list of dicts: job_post_id, email_received_date, cleaned, error.
    One browser session; login on first URL.
    """
    from utils.job_content_parser import parse_job_content_txt

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [
            {
                "job_post_id": r.get("job_post_id", ""),
                "email_received_date": r.get("date"),
                "view_job_link": (r.get("view_job_link") or "").strip(),
                "cleaned": {},
                "error": "playwright not installed",
            }
            for r, _ in rows_with_email_scrape_id
        ]

    results = []
    total = len(rows_with_email_scrape_id)
    if total == 0:
        return results

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(NAV_TIMEOUT_MS)

        # Phase 1 — login via canonical URL (stable, no redirect chain).
        first_row = rows_with_email_scrape_id[0][0]
        first_url = (first_row.get("view_job_link") or "").strip()
        login_url = canonical_kimedics_url(first_row.get("job_post_id", "")) or first_url
        _navigate_and_login_if_needed(page, login_url, kimedics_email, kimedics_password)

        # Phase 2 — visit each URL.
        for idx, (row, email_scrape_id) in enumerate(rows_with_email_scrape_id, 1):
            job_id = row.get("job_post_id", "")
            url = (row.get("view_job_link") or "").strip()
            email_date = row.get("date")
            try:
                body_text, desc_value = visit_and_extract(
                    page, url, job_id, kimedics_email, kimedics_password,
                )
                if desc_value:
                    body_text = body_text + "\n\n--- Description (full text) ---\n" + desc_value
                # Check if we got valid content or authentication failed
                if _is_logged_out_page(body_text):
                    error_msg = "AUTHENTICATION_FAILED: Detected 'Sign Out' - not logged in"
                    print(f"  ERROR for job {job_id}: {error_msg}", file=sys.stderr)
                    results.append({
                        "job_post_id": job_id,
                        "email_received_date": email_date,
                        "view_job_link": url,
                        "cleaned": {},
                        "error": error_msg,
                        "authentication_failed": True,
                    })
                elif not body_text.strip():
                    error_msg = "NO_CONTENT: Failed to extract any content after retries"
                    print(f"  ERROR for job {job_id}: {error_msg}", file=sys.stderr)
                    results.append({
                        "job_post_id": job_id,
                        "email_received_date": email_date,
                        "view_job_link": url,
                        "cleaned": {},
                        "error": error_msg,
                        "authentication_failed": True,
                    })
                elif not _looks_like_job_content(body_text):
                    # Parse it anyway but flag as suspicious
                    cleaned = parse_job_content_txt(body_text)
                    print(f"  WARNING for job {job_id}: Content doesn't match expected format", file=sys.stderr)
                    results.append({
                        "job_post_id": job_id,
                        "email_received_date": email_date,
                        "view_job_link": url,
                        "cleaned": cleaned,
                        "error": "SUSPICIOUS_CONTENT: Parsed but doesn't match expected format",
                        "authentication_failed": False,
                    })
                else:
                    # Success - valid content
                    cleaned = parse_job_content_txt(body_text)
                    results.append({
                        "job_post_id": job_id,
                        "email_received_date": email_date,
                        "view_job_link": url,
                        "cleaned": cleaned,
                    "error": "",
                    "email_scrape_id": email_scrape_id,
                })
            except Exception as e:
                results.append({
                    "job_post_id": job_id,
                    "email_received_date": email_date,
                    "view_job_link": url,
                    "cleaned": {},
                    "error": str(e)[:200],
                    "email_scrape_id": email_scrape_id,
                })
            if idx < total:
                time.sleep(BETWEEN_URLS_S)

        browser.close()

    return results
