"""
Tests for chatbot.email_backend.SESBackend (boto3 mocked, no network).

Raw send so Django builds the MIME message (Reply-To and all) and SES only
delivers it. FromEmailAddress is passed explicitly because the IAM policy
conditions on ses:FromAddress, which a raw send does not otherwise set.
"""

from unittest.mock import MagicMock, patch

import pytest
from django.core.mail import EmailMessage

from chatbot.email_backend import SESBackend


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
