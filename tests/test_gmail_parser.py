"""
Tests for Kimedics email parsing (parse_kimedics_job_email, link extraction).

No network or IMAP; uses inline HTML/subject/body. Run from project root:
  pytest tests/test_gmail_parser.py -v
"""

import pytest

from utils.gmail import (
    parse_kimedics_job_email,
    get_body_from_message,
    html_to_plain,
)


def test_parse_job_post_id():
    # "job post: #19440" style
    record = {"subject": "Job post: #19440", "body": "", "html": ""}
    out = parse_kimedics_job_email(record)
    assert out["job_post_id"] == "19440"

    record2 = {"subject": "New job post from #12345", "body": "At 4143 - Greenville", "html": ""}
    out2 = parse_kimedics_job_email(record2)
    assert out2["job_post_id"] == "12345"


def test_parse_location():
    record = {
        "subject": "Job #1",
        "body": "at 4143 - Greenville, NC (GREENVILLE, NC) some text",
        "html": "",
    }
    out = parse_kimedics_job_email(record)
    assert "4143" in out["location"]
    assert "Greenville" in out["location"]


def test_parse_view_job_link():
    html = '<a href="https://app.kimedics.com/jobs/123">View job post</a>'
    record = {"subject": "Job #1", "body": "", "html": html}
    out = parse_kimedics_job_email(record)
    assert out["view_job_link"] == "https://app.kimedics.com/jobs/123"


def test_parse_accept_to_submit_link():
    html = '<a href="https://portal.example.com/accept/456">Accept to submit providers</a>'
    record = {"subject": "Job #2", "body": "", "html": html}
    out = parse_kimedics_job_email(record)
    assert out["view_job_link"] == "https://portal.example.com/accept/456"


def test_parse_action_updated():
    record = {"subject": "Updated", "body": "Someone updated the job post", "html": ""}
    out = parse_kimedics_job_email(record)
    assert out["action_or_change"] == "updated"


def test_parse_action_new():
    record = {"subject": "New", "body": "New job post from practice", "html": ""}
    out = parse_kimedics_job_email(record)
    assert out["action_or_change"] == "new"


def test_html_to_plain():
    assert html_to_plain("<p>Hello</p>") == "Hello"
    assert html_to_plain("<a href='x'>Link</a> text") == "Link text"
    assert html_to_plain("") == ""


def test_get_body_from_message_plain():
    from email.message import Message
    msg = Message()
    msg.set_payload("Plain text body", charset="utf-8")
    msg.set_type("text/plain")
    assert "Plain text body" in get_body_from_message(msg)
