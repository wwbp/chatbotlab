"""
Tests for get_presigned_url (GET /api/avatar-upload/).

Coverage:
  - 403 when unauthenticated
  - 400 when filename or content_type is missing
  - 415 when content_type is not an allowed image type
  - 400 when filename contains unsafe characters (path traversal etc.)
  - 200 returns s3_url and file_url when boto3 succeeds (mocked)
  - 500 when boto3 raises an exception
"""

import uuid
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import Client, TestCase

UPLOAD_URL = "/api/avatar-upload/"


class TestGetPresignedUrl(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username=f"staff_{uuid.uuid4().hex[:6]}",
            password="pass",
            is_staff=True,
            is_superuser=True,
            is_active=True,
        )

    def _staff_client(self):
        c = Client()
        c.force_login(self.staff)
        return c

    def _get(self, client, params):
        return client.get(UPLOAD_URL, params)

    # ── Auth guard ─────────────────────────────────────────────────────────────

    def test_unauthenticated_returns_403(self):
        r = Client().get(
            UPLOAD_URL, {"filename": "a.jpg", "content_type": "image/jpeg"}
        )
        assert r.status_code == 403

    # ── Input validation ───────────────────────────────────────────────────────

    def test_missing_filename_returns_400(self):
        r = self._get(self._staff_client(), {"content_type": "image/jpeg"})
        assert r.status_code == 400
        assert "error" in r.json()

    def test_missing_content_type_returns_400(self):
        r = self._get(self._staff_client(), {"filename": "photo.jpg"})
        assert r.status_code == 400
        assert "error" in r.json()

    def test_unsupported_content_type_returns_415(self):
        r = self._get(
            self._staff_client(),
            {"filename": "clip.mp4", "content_type": "video/mp4"},
        )
        assert r.status_code == 415

    def test_filename_with_invalid_chars_returns_400(self):
        r = self._get(
            self._staff_client(),
            {"filename": "file<script>.jpg", "content_type": "image/jpeg"},
        )
        assert r.status_code == 400

    # ── Happy path ─────────────────────────────────────────────────────────────

    def test_valid_request_returns_s3_url_and_file_url(self):
        mock_s3 = MagicMock()
        mock_s3.generate_presigned_url.return_value = "https://s3.example.com/presigned"
        with (
            patch("chatbot.services.upload.boto3.client", return_value=mock_s3),
            patch(
                "chatbot.services.upload.os.getenv",
                side_effect=lambda k, *_: (
                    "test-bucket" if k == "AWS_BUCKET_NAME" else "us-east-1"
                ),
            ),
            patch("django.conf.settings.BACKEND_ENVIRONMENT", "local"),
        ):
            r = self._get(
                self._staff_client(),
                {"filename": "avatar.jpg", "content_type": "image/jpeg"},
            )
        assert r.status_code == 200
        body = r.json()
        assert "s3_url" in body
        assert "file_url" in body

    def test_boto3_failure_returns_500(self):
        with (
            patch(
                "chatbot.services.upload.boto3.client",
                side_effect=Exception("no creds"),
            ),
            patch("django.conf.settings.BACKEND_ENVIRONMENT", "local"),
        ):
            r = self._get(
                self._staff_client(),
                {"filename": "avatar.jpg", "content_type": "image/jpeg"},
            )
        assert r.status_code == 500
