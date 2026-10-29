"""
Tests for scrape_emails_from_sender's Gmail API transport
(_scrape_via_gmail_api): per-message fault isolation.

No real network; the Gmail service object is faked via monkeypatch.
Run from project root: pytest tests/test_gmail_api_transport.py -v
"""

from datetime import datetime, timezone
from email.message import EmailMessage
import base64

import pytest

from utils import gmail


def _raw_message(subject: str, body: str, from_: str = "jobs@kimedics.com") -> str:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_
    msg["Date"] = "Mon, 1 Sep 2026 10:00:00 +0000"
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


class _FakeHttpError(Exception):
    """Stand-in for googleapiclient.errors.HttpError (400 Precondition check failed)."""

    def __init__(self, status: int, reason: str):
        super().__init__(f"<HttpError {status} ... \"{reason}\".>")
        self.resp = type("Resp", (), {"status": status})()


class _FakeMessagesResource:
    """Mimics svc.users().messages() -- list() returns two ids; get() 400s on the
    first (simulating a message deleted/expunged between list and get, the exact
    shape of the 2026-10-29 production failure: HttpError 400 Precondition check
    failed on messages.get) and succeeds on the second."""

    def __init__(self, bad_id: str, good_id: str, good_raw: str):
        self.bad_id = bad_id
        self.good_id = good_id
        self.good_raw = good_raw
        self.get_calls: list[str] = []

    def list(self, **kwargs):
        return _Exec({"messages": [{"id": self.bad_id}, {"id": self.good_id}]})

    def get(self, *, userId, id, format):
        self.get_calls.append(id)
        if id == self.bad_id:
            return _Exec(None, raise_=_FakeHttpError(400, "Precondition check failed"))
        return _Exec({"raw": self.good_raw})


class _Exec:
    def __init__(self, value, raise_=None):
        self._value = value
        self._raise = raise_

    def execute(self):
        if self._raise is not None:
            raise self._raise
        return self._value


class _FakeUsers:
    def __init__(self, messages_resource):
        self._messages = messages_resource

    def messages(self):
        return self._messages


class _FakeService:
    def __init__(self, messages_resource):
        self._users = _FakeUsers(messages_resource)

    def users(self):
        return self._users


@pytest.fixture(autouse=True)
def _stub_oauth(monkeypatch):
    # Credentials.refresh() would hit the real network -- no-op it.
    monkeypatch.setattr(
        "google.oauth2.credentials.Credentials.refresh", lambda self, request: None
    )


def test_one_message_400_does_not_lose_the_rest_of_the_batch(monkeypatch):
    # The real 2026-10-29 failure: messages.get() 400'd on ONE message id
    # (HttpError: Precondition check failed), and that single exception
    # propagated all the way up and crashed scrape_gmail_job for the whole
    # run -- the good message right behind it in the same batch was never
    # even attempted.
    good_raw = _raw_message("Job post: #1", "at 1 - Townsville")
    resource = _FakeMessagesResource(bad_id="bad1", good_id="good1", good_raw=good_raw)
    monkeypatch.setattr(
        "googleapiclient.discovery.build", lambda *a, **k: _FakeService(resource)
    )

    oauth_cfg = {"client_id": "x", "client_secret": "y", "refresh_token": "z"}
    records = gmail._scrape_via_gmail_api(
        oauth_cfg, from_email="jobs@kimedics.com", cutoff_date=None, max_results=None
    )

    # The good message after the bad one was still fetched and returned.
    assert len(records) == 1
    assert records[0]["subject"] == "Job post: #1"
    # Both ids were attempted -- the bad one didn't stop the loop.
    assert resource.get_calls == ["bad1", "good1"]
