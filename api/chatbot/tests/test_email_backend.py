"""
Tests for chatbot.email_backend.SESBackend (boto3 mocked, no network).

Raw send so Django builds the MIME message (Reply-To and all) and SES only
delivers it. FromEmailAddress is passed explicitly because the IAM policy
conditions on ses:FromAddress, which a raw send does not otherwise set.
"""

import unittest
from unittest.mock import MagicMock, patch

import pytest
from django.core.mail import EmailMessage

from chatbot.email_backend import SESBackend, describe_error


def message():
    return EmailMessage(
        "Subject",
        "Body",
        "ChatbotLab <no-reply@bot.wwbp.org>",
        ["a@upenn.edu"],
        reply_to=["pi@upenn.edu"],
    )


@pytest.fixture
def ses():
    client = MagicMock()
    with patch("chatbot.email_backend.boto3.client", return_value=client) as factory:
        yield factory, client


def test_sends_raw_bytes_with_explicit_from_and_counts(ses, monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    factory, client = ses
    msg = message()
    assert SESBackend().send_messages([msg, message()]) == 2

    factory.assert_called_once_with("sesv2", region_name="us-east-2")
    kwargs = client.send_email.call_args.kwargs
    assert kwargs["FromEmailAddress"] == "no-reply@bot.wwbp.org"
    assert kwargs["Destination"] == {"ToAddresses": ["a@upenn.edu"]}
    raw = kwargs["Content"]["Raw"]["Data"]
    assert isinstance(raw, bytes)
    assert b"Reply-To: pi@upenn.edu" in raw


def test_region_defaults_to_us_east_1(ses, monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    factory, _ = ses
    SESBackend().send_messages([message()])
    factory.assert_called_once_with("sesv2", region_name="us-east-1")


def test_errors_raise_unless_fail_silently(ses):
    _, client = ses
    client.send_email.side_effect = RuntimeError("throttled")
    with pytest.raises(RuntimeError):
        SESBackend().send_messages([message()])
    assert SESBackend(fail_silently=True).send_messages([message()]) == 0


def test_silent_failure_is_still_logged_without_the_address(ses):
    _, client = ses
    client.send_email.side_effect = RuntimeError("rejected: a@upenn.edu")
    with unittest.TestCase().assertLogs("chatbot.email_backend", "ERROR") as logs:
        assert SESBackend(fail_silently=True).send_messages([message()]) == 0
    (line,) = logs.output
    assert "RuntimeError" in line
    assert "Subject" in line
    assert "upenn.edu" in line
    assert "a@upenn.edu" not in line


@pytest.mark.parametrize(
    ("raw", "redacted"),
    [
        ("to a@upenn.edu", "to ***@upenn.edu"),
        ("a@x.com,b@y.org", "***@x.com,***@y.org"),
        ("see mailto:a@x.com", "see mailto:***@x.com"),
        ('"Smith, John" <j@x.com>', "***@x.com>"),
        ('"john smith"@x.com', "***@x.com"),
    ],
    ids=["plain", "comma-separated", "mailto", "quoted-name", "quoted-local"],
)
def test_describe_error_keeps_only_the_domain(raw, redacted):
    assert describe_error(ValueError(raw)) == f"ValueError: {redacted}"
