"""
Tests for the link scraper (fetch_url, detect_login_required, extract_main_text, scrape_link).

Uses unittest.mock to avoid real HTTP requests. Run from project root:
  pytest tests/test_link_scraper.py -v
  python -m pytest tests/test_link_scraper.py -v
"""

from unittest.mock import patch, MagicMock

import pytest

from utils.link_scraper import (
    fetch_url,
    detect_login_required,
    extract_main_text,
    scrape_link,
    scrape_link_with_login,
    _cookie_header,
)


# -----------------------------------------------------------------------------
# _cookie_header
# -----------------------------------------------------------------------------


def test_cookie_header_empty():
    assert _cookie_header(None) == ""
    assert _cookie_header("") == ""
    assert _cookie_header({}) == ""


def test_cookie_header_dict():
    assert _cookie_header({"a": "1", "b": "2"}) == "a=1; b=2"
    assert _cookie_header({"session": "abc123"}) == "session=abc123"


def test_cookie_header_string():
    assert _cookie_header("session=abc; path=/") == "session=abc; path=/"


# -----------------------------------------------------------------------------
# fetch_url (mocked)
# -----------------------------------------------------------------------------


def test_fetch_url_success():
    body = b"<html><body>Hello</body></html>"
    resp = MagicMock()
    resp.getcode.return_value = 200
    resp.geturl.return_value = "https://example.com/page"
    resp.read.return_value = body
    # urlopen is used as "with urlopen(...) as resp", so mock must be a context manager
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = None

    with patch("utils.link_scraper.urlopen", return_value=cm):
        out = fetch_url("https://example.com/page")
    assert out["success"] is True
    assert out["status_code"] == 200
    assert out["body"] == body.decode("utf-8")
    assert out["final_url"] == "https://example.com/page"
    assert out["error"] == ""


def test_fetch_url_invalid_url():
    out = fetch_url("")
    assert out["success"] is False
    assert "Invalid" in out["error"] or out["error"]

    out = fetch_url("ftp://example.com")
    assert out["success"] is False


def test_fetch_url_with_cookies():
    body = b"<html>OK</html>"
    resp = MagicMock()
    resp.getcode.return_value = 200
    resp.geturl.return_value = "https://example.com/"
    resp.read.return_value = body
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = None

    with patch("utils.link_scraper.urlopen", return_value=cm) as m:
        fetch_url("https://example.com/", cookies={"session": "abc"})
    call_args = m.call_args
    req = call_args[0][0] if call_args[0] else call_args.kwargs.get("request")
    assert req is not None
    assert "Cookie" in req.headers
    assert "session=abc" in req.headers["Cookie"]


# -----------------------------------------------------------------------------
# detect_login_required
# -----------------------------------------------------------------------------


def test_detect_login_required_url_has_login():
    assert detect_login_required("anything", "https://example.com/login") is True
    assert detect_login_required("", "https://example.com/signin") is True
    assert detect_login_required("", "https://example.com/auth/sso") is True


def test_detect_login_required_url_clean():
    assert detect_login_required("", "https://example.com/jobs/123") is False


def test_detect_login_required_content_has_phrase():
    assert detect_login_required("Please sign in to continue", "https://example.com/page") is True
    assert detect_login_required("Log in to your account", "https://example.com/page") is True
    assert detect_login_required("Session expired", "https://example.com/page") is True


def test_detect_login_required_content_clean():
    html = "<html><body><main>Job title: Dentist needed</main></body></html>"
    assert detect_login_required(html, "https://example.com/job/1") is False


def test_detect_login_required_spa_shell():
    # JS-rendered app: small HTML shell, no "sign in" in raw HTML; URL has /app/workspace/job-posts/
    small_html = "<!DOCTYPE html><html><body><div id=root></div><script src=app.js></script></body></html>"
    assert detect_login_required(small_html, "https://portal.kimedics.com/app/workspace/job-posts/19422") is True
    assert detect_login_required(small_html, "https://example.com/app/dashboard") is True


# -----------------------------------------------------------------------------
# extract_main_text
# -----------------------------------------------------------------------------


def test_extract_main_text_empty():
    assert extract_main_text("") == ""
    assert extract_main_text("<script>x</script>") == ""


def test_extract_main_text_simple():
    html = "<html><body><p>Hello world</p></body></html>"
    assert "Hello world" in extract_main_text(html)


def test_extract_main_text_strips_script_style():
    html = "<html><body><script>alert(1)</script><p>Visible</p></body></html>"
    text = extract_main_text(html)
    assert "Visible" in text
    assert "alert" not in text


def test_extract_main_text_prefers_main():
    html = "<html><body><nav>Nav</nav><main>Main content</main></body></html>"
    text = extract_main_text(html)
    assert "Main content" in text


# -----------------------------------------------------------------------------
# scrape_link (mocked)
# -----------------------------------------------------------------------------


def test_scrape_link_success():
    html = "<html><body><main>Job description here</main></body></html>"
    resp = MagicMock()
    resp.getcode.return_value = 200
    resp.geturl.return_value = "https://example.com/job/1"
    resp.read.return_value = html.encode("utf-8")
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = None

    with patch("utils.link_scraper.urlopen", return_value=cm):
        out = scrape_link("https://example.com/job/1")
    assert out["success"] is True
    assert out["login_required"] is False
    assert "Job description" in out["content_text"]
    assert out["content_length"] > 0


def test_scrape_link_login_required():
    html = "<html><body>Please log in to continue</body></html>"
    resp = MagicMock()
    resp.getcode.return_value = 200
    resp.geturl.return_value = "https://example.com/login"
    resp.read.return_value = html.encode("utf-8")
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = None

    with patch("utils.link_scraper.urlopen", return_value=cm):
        out = scrape_link("https://example.com/job/1")
    assert out["login_required"] is True
    assert out["content_text"] == ""


def test_scrape_link_invalid_url():
    out = scrape_link("not-a-url")
    assert out["success"] is False
    assert out["error"]


def test_scrape_link_with_login_retries_with_cookies():
    # First response: login page; second (with cookies): real content
    login_html = "<html><body>Sign in</body></html>"
    content_html = "<html><body><main>Real job content</main></body></html>"

    resp1 = MagicMock()
    resp1.getcode.return_value = 200
    resp1.geturl.return_value = "https://example.com/login"
    resp1.read.return_value = login_html.encode("utf-8")

    resp2 = MagicMock()
    resp2.getcode.return_value = 200
    resp2.geturl.return_value = "https://example.com/job/1"
    resp2.read.return_value = content_html.encode("utf-8")

    def urlopen_side_effect(req, *args, **kwargs):
        r = resp2 if req.headers.get("Cookie") else resp1
        cm = MagicMock()
        cm.__enter__.return_value = r
        cm.__exit__.return_value = None
        return cm

    with patch("utils.link_scraper.urlopen", side_effect=urlopen_side_effect):
        out = scrape_link_with_login(
            "https://example.com/job/1",
            get_cookies=lambda: {"session": "abc"},
        )
    assert out["login_required"] is False
    assert "Real job content" in out["content_text"]


def test_scrape_link_with_login_no_cookies_returns_first_result():
    login_html = "<html><body>Sign in</body></html>"
    resp = MagicMock()
    resp.getcode.return_value = 200
    resp.geturl.return_value = "https://example.com/login"
    resp.read.return_value = login_html.encode("utf-8")
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = None

    with patch("utils.link_scraper.urlopen", return_value=cm):
        out = scrape_link_with_login("https://example.com/job/1", get_cookies=lambda: None)
    assert out["login_required"] is True
    assert out["content_text"] == ""
