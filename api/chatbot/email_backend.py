"""
Email backend that sends through Amazon SES (v2 API) with the instance role.

Raw send: Django builds the whole MIME message (headers, Reply-To) and SES only
delivers it. FromEmailAddress is passed explicitly because the IAM policy
conditions on ses:FromAddress. boto3 is already a dependency, so no django-ses.
"""

import os
from email.utils import parseaddr

import boto3
from django.core.mail.backends.base import BaseEmailBackend


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
            except Exception:
                if not self.fail_silently:
                    raise
            else:
                sent += 1
        return sent
