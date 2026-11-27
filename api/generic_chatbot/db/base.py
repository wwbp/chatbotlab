"""Django's MySQL backend, re-reading a rotated RDS master password.

RDS rotates the password it keeps in Secrets Manager (every 7 days by
default) and the old one stops working at once, while each process read it
at start (aws_secrets.py). A connection refused with 1045 (access denied)
reads the secret again and tries once more. Without DATABASE_SECRET_ID
(local development, CI) this is the stock backend.
"""

import os

from django.db.backends.mysql import base

from generic_chatbot import aws_secrets

ACCESS_DENIED = 1045


class DatabaseWrapper(base.DatabaseWrapper):
    def get_new_connection(self, conn_params):
        try:
            return super().get_new_connection(conn_params)
        except base.Database.OperationalError as err:
            secret_id = os.getenv("DATABASE_SECRET_ID")
            if not secret_id or err.args[0] != ACCESS_DENIED:
                raise
            aws_secrets.read.cache_clear()
            db = aws_secrets.read(secret_id)
            self.settings_dict["USER"], self.settings_dict["PASSWORD"] = (
                db["username"],
                db["password"],
            )
            return super().get_new_connection(
                {**conn_params, "user": db["username"], "password": db["password"]}
            )
