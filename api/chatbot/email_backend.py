"""
Email backend that sends through Amazon SES (v2 API) with the instance role.

Raw send: Django builds the whole MIME message (headers, Reply-To) and SES only
delivers it. FromEmailAddress is passed explicitly because the IAM policy
conditions on ses:FromAddress. boto3 is already a dependency, so no django-ses.
"""

import logging
import os
import re
from email.utils import parseaddr

import boto3
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)


def describe_error(error):
    """The error class and message with any address cut to its domain.

    SES names the rejected address in its message, e.g. "... failed the check
    in region US-EAST-1: a@upenn.edu"; logs keep only "***@upenn.edu".
    """
    # A quoted name or local part in front goes too: "Smith, John" <j@x.com>.
    text = re.sub(r'(?:"[^"]*"\s*<?)?[^\s@:;,<>"]*@', "***@", str(error))
    return f"{type(error).__name__}: {text}"


class SESBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        client = boto3.client("sesv2", region_name=os.getenv("AWS_REGION", "us-east-1"))
        sent = 0
        for message in email_messages:
            try:
                client.send_email(
                    FromEmailAddress=parseaddr(message.from_email)[1],
                    Destination={"ToAddresses": message.recipients()},
                    Content={"Raw": {"Data": message.message().as_bytes()}},
                )
            except Exception as error:
                # Subject = purpose; domains only, never the address or body.
                # Subjects are logged, so they must not contain personal data.
                logger.error(
                    "email send failed (%s) to %s: %s",
                    message.subject,
                    ", ".join(
                        sorted({r.rpartition("@")[2] for r in message.recipients()})
                    ),
                    describe_error(error),
                )
                if not self.fail_silently:
                    raise
            else:
                sent += 1
        return sent
