"""
July 2 regression: a Modal hard kill mid-batch discarded every scraped page
because nothing was persisted until the whole batch finished. scrape_job_pages
now (a) reports each result via on_result the moment it exists, and (b) stops
at a soft deadline instead of running into the kill.
"""
import time
from types import SimpleNamespace

import playwright.sync_api

import utils.playwright_job_scrape as pjs

_FAKE_BODY = (
    "Job Post Details\nPractice 123 - Dublin, GA\nStatus Active, accepting new providers\n"
    "Posted On 07/01/2026\nSpecialty General Dentistry\n"
)


class _FakePage:
    def set_default_timeout(self, ms):
        pass


class _FakeBrowser:
    def new_page(self):
        return _FakePage()

    def close(self):
        pass


class _FakeCtx:
    def __enter__(self):
        return SimpleNamespace(chromium=SimpleNamespace(launch=lambda headless=True: _FakeBrowser()))

    def __exit__(self, *a):
        return False


def _rows(n):
    return [
        ({"job_post_id": str(20000 + i), "view_job_link": f"http://k/{i}", "date": None}, i)
        for i in range(n)
    ]


def _wire(monkeypatch, trace):
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: _FakeCtx())
    monkeypatch.setattr(pjs, "_navigate_and_login_if_needed", lambda *a, **k: None)
    monkeypatch.setattr(pjs, "_load_sf_practice_map_for_parser", lambda: {})
    monkeypatch.setattr(pjs, "BETWEEN_URLS_S", 0)

    def fake_visit(page, url, job_id, email, password):
        trace.append(("visit", job_id))
        return _FAKE_BODY, ""

    monkeypatch.setattr(pjs, "visit_and_extract", fake_visit)


def test_on_result_fires_per_page_before_next_visit(monkeypatch):
    trace = []
    _wire(monkeypatch, trace)

    results = pjs.scrape_job_pages(
        _rows(3), "e@x.com", "pw",
        on_result=lambda r: trace.append(("save", r["job_post_id"])),
    )

    assert len(results) == 3
    # Each page is saved before the next page is visited — the incremental guarantee.
    assert trace == [
        ("visit", "20000"), ("save", "20000"),
        ("visit", "20001"), ("save", "20001"),
        ("visit", "20002"), ("save", "20002"),
    ]


def test_deadline_stops_loop_without_error(monkeypatch):
    trace = []
    _wire(monkeypatch, trace)

    results = pjs.scrape_job_pages(
        _rows(5), "e@x.com", "pw",
        deadline_ts=time.monotonic() - 1,  # already expired
    )

    assert results == []          # nothing scraped, nothing raised
    assert trace == []            # no page visited past the deadline


def test_callback_error_never_breaks_the_scrape(monkeypatch):
    trace = []
    _wire(monkeypatch, trace)

    def exploding(r):
        trace.append(("save", r["job_post_id"]))
        raise RuntimeError("db down")

    results = pjs.scrape_job_pages(_rows(3), "e@x.com", "pw", on_result=exploding)

    assert len(results) == 3
    assert [t for t in trace if t[0] == "save"] == [
        ("save", "20000"), ("save", "20001"), ("save", "20002"),
    ]
