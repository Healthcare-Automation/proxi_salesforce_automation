"""
July 10 regression: Gmail's 15-simultaneous-IMAP-connection cap rejected the
cron's login ("[ALERT] Too many simultaneous connections.") and the whole tick
failed on the first attempt. Login must retry through the transient cap, close
rejected sockets (they count against the cap), and always log out the session.
"""
import imaplib

import pytest

from utils import gmail
from utils.gmail import _login_with_retry, scrape_emails_from_sender

_CAP_ERROR = imaplib.IMAP4.error(b"[ALERT] Too many simultaneous connections. (Failure)")


class _FakeIMAP:
    """Scripted IMAP4_SSL stand-in: fails login N times, then succeeds."""

    instances: list = []
    login_failures_remaining = 0

    def __init__(self, host):
        self.host = host
        self.shutdown_called = False
        self.logout_called = False
        _FakeIMAP.instances.append(self)

    def login(self, user, password):
        if _FakeIMAP.login_failures_remaining > 0:
            _FakeIMAP.login_failures_remaining -= 1
            raise _CAP_ERROR

    def shutdown(self):
        self.shutdown_called = True

    def logout(self):
        self.logout_called = True

    def select(self, mailbox):
        return "OK", [b"1"]

    def search(self, charset, criteria):
        return "OK", [b""]


@pytest.fixture
def fake_imap(monkeypatch):
    _FakeIMAP.instances = []
    _FakeIMAP.login_failures_remaining = 0
    # Tests override login/search at class level — undo it after each test.
    monkeypatch.setattr(_FakeIMAP, "login", _FakeIMAP.login)
    monkeypatch.setattr(_FakeIMAP, "search", _FakeIMAP.search)
    monkeypatch.setattr(gmail.imaplib, "IMAP4_SSL", _FakeIMAP)
    monkeypatch.setattr(gmail.time, "sleep", lambda s: None)
    return _FakeIMAP


def test_login_retries_through_transient_cap(fake_imap):
    fake_imap.login_failures_remaining = 2
    mail = _login_with_retry("imap.test", "a@b.com", "pw")
    assert isinstance(mail, _FakeIMAP)
    assert len(fake_imap.instances) == 3
    # Every rejected connection's socket was closed, not left dangling.
    assert all(m.shutdown_called for m in fake_imap.instances[:-1])
    assert not fake_imap.instances[-1].shutdown_called


def test_login_gives_up_after_max_attempts(fake_imap):
    fake_imap.login_failures_remaining = 99
    with pytest.raises(imaplib.IMAP4.error):
        _login_with_retry("imap.test", "a@b.com", "pw")
    assert len(fake_imap.instances) == 3


def test_non_transient_login_error_raises_immediately(fake_imap):
    def bad_credentials(self, user, password):
        raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")

    fake_imap.login_failures_remaining = 0
    fake_imap.login = bad_credentials
    with pytest.raises(imaplib.IMAP4.error):
        _login_with_retry("imap.test", "a@b.com", "pw")
    assert len(fake_imap.instances) == 1


def test_scrape_always_logs_out_even_when_search_raises(fake_imap):
    def boom(self, charset, criteria):
        raise imaplib.IMAP4.error(b"transient search failure")

    fake_imap.search = boom
    with pytest.raises(imaplib.IMAP4.error):
        scrape_emails_from_sender("a@b.com", "pw", "sender@x.com", imap_server="imap.test")
    assert fake_imap.instances[-1].logout_called


def test_scrape_logs_out_on_empty_result(fake_imap):
    result = scrape_emails_from_sender("a@b.com", "pw", "sender@x.com", imap_server="imap.test")
    assert result == []
    assert fake_imap.instances[-1].logout_called
