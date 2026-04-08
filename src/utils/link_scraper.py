"""
Link content scraper: fetch a URL and extract readable text from the page.

Purpose
-------
Job emails from Kimedics include a "View job post" link (view_job_link). This module
fetches that URL and scrapes the page content so we have the full job description
instead of only the email snippet.

Logic overview
--------------
1. FETCH: We perform an HTTP GET request to the URL (using only the standard library).
   - We follow redirects (up to a limit) so login redirects are visible.
   - We send a browser-like User-Agent so some sites don't block us.

2. DETECT LOGIN: Many job portals show the job only when you're logged in. After
   fetching, we check the response for common "login required" signals:
   - Final URL contains "login", "signin", "auth", "sso".
   - Page title or body contains phrases like "Sign in", "Log in", "session expired".
   - This gives us a boolean "login_required" so callers can handle it (e.g. skip,
     or later use browser automation with real credentials).

3. EXTRACT TEXT: From the HTML we strip scripts/styles, then take the main content
   (prefer <main> or <article>, else <body>) and get plain text. We normalize
   whitespace so the result is readable.

When login is required
----------------------
We do NOT implement login here (no credentials, no browser automation). Options
for later:
- Cookie-based: Log in once in a browser, export cookies, pass them into the
  request. Fragile (cookies expire).
- Headless browser: Use Playwright or Selenium, automate login with stored
  credentials, then scrape. More robust but heavier and needs secret storage.

All functions are pure and documented so you can trace every decision.
"""

import re
import ssl
from html.parser import HTMLParser
from typing import Callable, Optional, Union
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

# -----------------------------------------------------------------------------
# Constants (tunable)
# -----------------------------------------------------------------------------

# Max redirects so we don't loop forever (e.g. login page redirecting to itself).
MAX_REDIRECTS = 5

# User-Agent sent with the request. Some sites block empty or script-like UAs.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Phrases that strongly suggest the page is a login/gate page, not the job content.
LOGIN_INDICATOR_PHRASES = [
    "sign in",
    "log in",
    "login",
    "sign in to",
    "log in to",
    "session expired",
    "please log in",
    "please sign in",
    "authentication required",
    "you must be logged in",
]

# URL path segments that suggest we were redirected to a login/auth page.
LOGIN_URL_PATHS = ["login", "signin", "sign-in", "log-in", "auth", "sso", "oauth"]
# Path segments that suggest an app route (SPA). Small body = likely empty shell or login rendered by JS.
SPA_APP_PATH_SEGMENTS = ["/app/", "/workspace/", "/job-posts/", "/dashboard/"]
SPA_SHELL_MAX_BODY = 5000  # If body smaller than this and URL is app-like, treat as login required

# Max HTML size (chars) to look for meta-refresh / JS redirect. Tracking pages are usually tiny.
META_REFRESH_MAX_BODY = 5000
# Pattern for <meta http-equiv="refresh" content="0; url=..."> or content="0;url=..."
_META_REFRESH_RE = re.compile(
    r'<meta[^>]+http-equiv\s*=\s*["\']?refresh["\']?[^>]+content\s*=\s*["\']?\d+\s*;\s*url\s*=\s*([^"\'>\s]+)',
    re.IGNORECASE,
)
_META_REFRESH_RE2 = re.compile(
    r'content\s*=\s*["\']?\d+\s*;\s*url\s*=\s*([^"\'>\s]+)',
    re.IGNORECASE,
)
# Fallback: JS redirect e.g. window.location="https://..." or location.href='https://...'
_JS_LOCATION_RE = re.compile(
    r'(?:window\.location|location\.href)\s*=\s*["\'](https?://[^"\']+)["\']',
    re.IGNORECASE,
)


def _extract_meta_refresh_url(html: str) -> Optional[str]:
    """If the HTML has a meta refresh to another URL, return that URL (absolute). Else None."""
    if not html or len(html) > META_REFRESH_MAX_BODY:
        return None
    for pattern in (_META_REFRESH_RE, _META_REFRESH_RE2):
        m = pattern.search(html)
        if m:
            url = m.group(1).strip().strip("'\"")
            if url.startswith("http://") or url.startswith("https://"):
                return url
    m = _JS_LOCATION_RE.search(html)
    if m:
        url = m.group(1).strip()
        if url.startswith("http://") or url.startswith("https://"):
            return url
    return None


# -----------------------------------------------------------------------------
# Step 1: Fetch URL (with redirects + optional meta-refresh)
# -----------------------------------------------------------------------------


def _cookie_header(cookies: Union[dict, str, None]) -> str:
    """Turn cookies dict or 'name=value; ...' string into a Cookie header value."""
    if not cookies:
        return ""
    if isinstance(cookies, str):
        return cookies.strip()
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if k and v is not None)


def fetch_url(
    url: str,
    timeout_seconds: float = 15.0,
    cookies: Union[dict, str, None] = None,
) -> dict:
    """
    Fetch a URL with HTTP GET and return response metadata and body.

    Logic:
    - We use urllib.request (stdlib only; no extra dependencies).
    - We set a Request with User-Agent so servers don't treat us as a bot.
    - If cookies is provided (dict or "name=value; ..." string), add Cookie header.
    - We follow redirects by re-calling ourselves until we get a non-redirect
      or exceed MAX_REDIRECTS.

    Args:
        url: Full URL to fetch (e.g. from view_job_link).
        timeout_seconds: Request timeout.
        cookies: Optional. Dict of name->value or a single Cookie header string.

    Returns:
        Dict with: success, status_code, final_url, body, error.
    """
    result = {
        "success": False,
        "status_code": None,
        "final_url": url,
        "body": "",
        "error": "",
    }
    redirect_count = 0
    current_url = url
    cookie_val = _cookie_header(cookies)

    ctx = ssl.create_default_context()

    while redirect_count <= MAX_REDIRECTS:
        try:
            headers = {"User-Agent": USER_AGENT}
            if cookie_val:
                headers["Cookie"] = cookie_val
            req = Request(current_url, headers=headers)
            with urlopen(req, timeout=timeout_seconds, context=ctx) as resp:
                result["status_code"] = resp.getcode()
                result["final_url"] = resp.geturl()
                raw = resp.read()
                result["body"] = raw.decode("utf-8", errors="replace")
                result["success"] = 200 <= resp.getcode() < 300
                # Tracking pages (e.g. SendGrid) often return 200 with a tiny HTML + meta refresh
                meta_url = _extract_meta_refresh_url(result["body"])
                if meta_url and redirect_count < MAX_REDIRECTS:
                    redirect_count += 1
                    current_url = meta_url
                    continue
                return result
        except HTTPError as e:
            result["status_code"] = e.code
            result["final_url"] = e.url if getattr(e, "url", None) else current_url
            result["error"] = f"HTTP {e.code}: {e.reason}"
            # Redirect?
            if e.code in (301, 302, 303, 307, 308):
                redirect_count += 1
                location = e.headers.get("Location")
                if location:
                    current_url = location if location.startswith("http") else (current_url.rstrip("/") + "/" + location.lstrip("/"))
                    continue
            return result
        except URLError as e:
            result["error"] = str(e.reason) if e.reason else str(e)
            return result
        except TimeoutError as e:
            result["error"] = f"Timeout: {e}"
            return result
        except Exception as e:
            result["error"] = str(e)
            return result

    result["error"] = "Too many redirects"
    return result


# -----------------------------------------------------------------------------
# Step 2: Detect if the page is a login/gate page
# -----------------------------------------------------------------------------


def detect_login_required(html: str, final_url: str) -> bool:
    """
    Heuristic: does this page look like a login/gate page instead of job content?

    Logic:
    - If the final URL path contains any of LOGIN_URL_PATHS (e.g. /login, /signin),
      we assume we were redirected to login → login_required True.
    - We take a small slice of the HTML (first 20_000 chars) and lower-case it,
      then check if any of LOGIN_INDICATOR_PHRASES appear. This avoids scanning
      huge pages and catches "Sign in to continue" in title or body.
    - We do NOT look at status code alone: 200 can still be a login form.

    Args:
        html: Full HTML of the page.
        final_url: URL we ended up at (after redirects).

    Returns:
        True if we believe the user must log in to see the real content.
    """
    if not (html or final_url):
        return False

    # Check URL path (e.g. https://portal.example.com/login -> login)
    try:
        path = urlparse(final_url).path.lower()
    except Exception:
        path = final_url.lower()

    for segment in LOGIN_URL_PATHS:
        if segment in path:
            return True

    # Check page content for login phrases (only first N chars for speed)
    sample = (html or "")[:20_000].lower()
    for phrase in LOGIN_INDICATOR_PHRASES:
        if phrase in sample:
            return True

    # JS-rendered (SPA) login: server sends a tiny HTML shell; "Sign In" form is added by React/Vue.
    # So the raw HTML has no "sign in" text. If URL looks like an app route and body is small, assume gate.
    if len(html or "") < SPA_SHELL_MAX_BODY:
        for segment in SPA_APP_PATH_SEGMENTS:
            if segment in path:
                return True

    return False


# -----------------------------------------------------------------------------
# Step 3: Extract main text from HTML
# -----------------------------------------------------------------------------


class _TextExtractor(HTMLParser):
    """
    Collects text from HTML elements we care about, skipping script/style.
    Used only inside extract_main_text(); not part of the public API.
    """

    def __init__(self):
        super().__init__()
        self._in_script = False
        self._in_style = False
        self._parts = []
        self._main_parts = []
        self._main_article_depth = 0  # >0 when we're inside <main> or <article>

    def handle_starttag(self, tag, attrs):
        tag_lower = tag.lower()
        if tag_lower == "script":
            self._in_script = True
        elif tag_lower == "style":
            self._in_style = True
        if tag_lower in ("main", "article"):
            self._main_article_depth += 1

    def handle_endtag(self, tag):
        tag_lower = tag.lower()
        if tag_lower == "script":
            self._in_script = False
        elif tag_lower == "style":
            self._in_style = False
        if tag_lower in ("main", "article"):
            self._main_article_depth = max(0, self._main_article_depth - 1)

    def handle_data(self, data):
        if self._in_script or self._in_style:
            return
        text = data.strip()
        if not text:
            return
        self._parts.append(text)
        if self._main_article_depth > 0:
            self._main_parts.append(text)

    def get_text(self):
        # Prefer content we saw inside <main> or <article>; else full body text.
        use = self._main_parts if self._main_parts else self._parts
        return " ".join(use)

    def get_all_text(self):
        return " ".join(self._parts)


def extract_main_text(html: str, max_chars: int = 150_000) -> str:
    """
    Extract readable plain text from HTML, stripping scripts and normalizing space.

    Logic:
    - We use the stdlib HTMLParser to walk the document and collect text from
      non-script, non-style nodes.
    - We prefer text that appeared inside <main> or <article> if present (many
      sites put job content there). Otherwise we use all collected text (body).
    - We collapse whitespace to single spaces and trim so the result is one
      block of readable text. We cap length at max_chars to avoid huge strings.

    Args:
        html: Full HTML string.
        max_chars: Maximum length of returned string (default 150k).

    Returns:
        Single string of plain text, or "" if parsing failed.
    """
    if not html or not html.strip():
        return ""

    parser = _TextExtractor()
    try:
        parser.feed(html[:max_chars * 2])  # Feed a bit more so we have enough context
        text = parser.get_text()
    except Exception:
        text = ""
    if not text:
        try:
            parser2 = _TextExtractor()
            parser2.feed(html[:max_chars * 2])
            text = parser2.get_all_text()
        except Exception:
            text = ""

    # Normalize: collapse runs of whitespace to one space, trim.
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "..."
    return text


# -----------------------------------------------------------------------------
# Public API: one function that does fetch + detect login + extract text
# -----------------------------------------------------------------------------


def scrape_link(
    url: str,
    timeout_seconds: float = 15.0,
    cookies: Union[dict, str, None] = None,
) -> dict:
    """
    Fetch a URL, detect if it's a login page, and extract main text content.

    This is the main entry point. It runs the full pipeline:

    1. fetch_url(url)        → get HTML and final URL
    2. detect_login_required(html, final_url) → True if we think login is needed
    3. extract_main_text(html) → plain text for storage or display

    Args:
        url: The link to scrape (e.g. view_job_link from a job email).
        timeout_seconds: Timeout for the HTTP request.
        cookies: Optional. Dict or Cookie header string to send with the request.

    Returns:
        Dict with:
          - url: str — Original URL requested.
          - final_url: str — URL after redirects.
          - success: bool — True if we got a 2xx and could read the page.
          - status_code: int or None — HTTP status.
          - login_required: bool — True if the page looks like a login/gate page.
          - content_text: str — Extracted plain text (empty if not success or login).
          - error: str — Non-empty if request failed (timeout, 403, etc.).
          - content_length: int — Length of content_text (for quick checks).
    """
    out = {
        "url": url,
        "final_url": url,
        "success": False,
        "status_code": None,
        "login_required": False,
        "content_text": "",
        "error": "",
        "content_length": 0,
    }

    if not url or not url.strip().startswith(("http://", "https://")):
        out["error"] = "Invalid or empty URL"
        return out

    # Step 1: Fetch
    fetch_result = fetch_url(
        url.strip(),
        timeout_seconds=timeout_seconds,
        cookies=cookies,
    )
    out["final_url"] = fetch_result["final_url"]
    out["status_code"] = fetch_result["status_code"]
    out["error"] = fetch_result["error"]

    if not fetch_result["success"]:
        # Still set login_required if we were redirected to a login URL
        out["login_required"] = detect_login_required(
            fetch_result["body"], fetch_result["final_url"]
        )
        return out

    html = fetch_result["body"]

    # Step 2: Detect login
    out["login_required"] = detect_login_required(html, fetch_result["final_url"])
    if out["login_required"]:
        # We got 200 but the content is a login page; don't treat as useful content
        out["content_text"] = ""
        return out

    # Step 3: Extract text
    out["content_text"] = extract_main_text(html)
    out["content_length"] = len(out["content_text"])
    out["success"] = True
    return out


def scrape_link_with_login(
    url: str,
    get_cookies: Callable[[], Union[dict, str, None]],
    timeout_seconds: float = 15.0,
) -> dict:
    """
    Same as scrape_link, but if the first fetch returns login_required, call
    get_cookies() and retry the request with those cookies (e.g. from a prior
    login). Use this when you have credentials and a way to obtain session cookies.

    Args:
        url: The link to scrape.
        get_cookies: Callable that returns cookie dict, Cookie header string, or None.
        timeout_seconds: Request timeout.

    Returns:
        Same dict as scrape_link() (success, login_required, content_text, etc.).
    """
    result = scrape_link(url, timeout_seconds=timeout_seconds)
    if not result.get("login_required"):
        return result
    cookies = get_cookies() if callable(get_cookies) else None
    if not cookies:
        return result
    return scrape_link(url, timeout_seconds=timeout_seconds, cookies=cookies)
