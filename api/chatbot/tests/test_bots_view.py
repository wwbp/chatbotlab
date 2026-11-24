"""
Tests for ListBotsAPIView (GET/POST /api/bots/) and
BotDetailAPIView (GET/PUT/DELETE /api/bots/<pk>/).

Coverage:
  - Auth guard: unauthenticated requests → 403 on every endpoint
  - GET /api/bots/: staff returns 200 with bots list containing created bot
  - POST /api/bots/: missing fields → 400, bad JSON → 400, unknown model → 400,
    valid → 201 with expected fields
  - GET /api/bots/<pk>/: not found → 404, valid → 200 with full field set
  - PUT /api/bots/<pk>/: not found → 404, bad JSON → 400, valid → 200 + DB updated
  - DELETE /api/bots/<pk>/: not found → 404, valid → 204 + row gone (S3 call mocked)
"""

import json
import uuid
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase

from chatbot.models import Bot, Model

LIST_URL = "/api/bots/"
DETAIL_URL = "/api/bots/{}/"


class TestBotsView(TestCase):
    """HTTP-layer tests for the bots CRUD API.

    Uses TestCase so that both ORM writes and HTTP-client-committed writes are
    all wrapped in the outer transaction and cleaned up automatically.
    """

    def setUp(self):
        Model.get_or_create_default_models()
        self.model = Model.objects.get(provider__name="OpenAI", model_id="gpt-4o-mini")
        self.staff = User.objects.create_user(
            username=f"staff_{uuid.uuid4().hex[:6]}",
            password="pass",
            is_staff=True,
            is_superuser=True,
            is_active=True,
        )
        self.bot = Bot.objects.create(
            name=f"bot_{uuid.uuid4().hex[:8]}",
            prompt="Test prompt.",
            ai_model=self.model,
            model_type="OpenAI",
            model_id="gpt-4o-mini",
        )

    def _staff_client(self):
        c = Client()
        c.force_login(self.staff)
        return c

    def _post(self, client, data):
        return client.post(
            LIST_URL, data=json.dumps(data), content_type="application/json"
        )

    def _put(self, client, pk, data):
        return client.put(
            DETAIL_URL.format(pk),
            data=json.dumps(data),
            content_type="application/json",
        )

    # ── Auth guard ─────────────────────────────────────────────────────────────

    def test_list_get_unauthenticated_returns_403(self):
        r = Client().get(LIST_URL)
        assert r.status_code == 403

    def test_list_post_unauthenticated_returns_403(self):
        r = self._post(
            Client(), {"name": "x", "model_type": "OpenAI", "model_id": "gpt-4o-mini"}
        )
        assert r.status_code == 403

    def test_detail_get_unauthenticated_returns_403(self):
        r = Client().get(DETAIL_URL.format(self.bot.pk))
        assert r.status_code == 403

    def test_detail_put_unauthenticated_returns_403(self):
        r = self._put(Client(), self.bot.pk, {"name": "x"})
        assert r.status_code == 403

    def test_detail_delete_unauthenticated_returns_403(self):
        r = Client().delete(DETAIL_URL.format(self.bot.pk))
        assert r.status_code == 403

    # ── GET /api/bots/ ─────────────────────────────────────────────────────────

    def test_list_returns_200_with_bots_key(self):
        r = self._staff_client().get(LIST_URL)
        assert r.status_code == 200
        data = r.json()
        assert "bots" in data
        assert isinstance(data["bots"], list)

    def test_list_includes_created_bot(self):
        r = self._staff_client().get(LIST_URL)
        ids = [b["id"] for b in r.json()["bots"]]
        assert self.bot.id in ids

    # ── POST /api/bots/ ────────────────────────────────────────────────────────

    def test_post_missing_fields_returns_400(self):
        r = self._post(self._staff_client(), {"name": "only_name"})
        assert r.status_code == 400
        assert "error" in r.json()

    def test_post_invalid_json_returns_400(self):
        r = self._staff_client().post(
            LIST_URL, data="not-json", content_type="application/json"
        )
        assert r.status_code == 400

    def test_post_unknown_model_returns_400(self):
        r = self._post(
            self._staff_client(),
            {"name": "x", "model_type": "Unknown", "model_id": "no-such"},
        )
        assert r.status_code == 400
        assert "No model found" in r.json()["error"]

    def test_post_creates_bot_returns_201(self):
        name = f"new_{uuid.uuid4().hex[:6]}"
        r = self._post(
            self._staff_client(),
            {
                "name": name,
                "model_type": "OpenAI",
                "model_id": "gpt-4o-mini",
                "prompt": "Hello.",
            },
        )
        assert r.status_code == 201
        body = r.json()
        assert body["name"] == name
        for field in (
            "id",
            "model_type",
            "model_id",
            "prompt",
            "initial_utterance",
            "avatar_type",
        ):
            assert field in body, f"missing field: {field}"

    # ── GET /api/bots/<pk>/ ────────────────────────────────────────────────────

    def test_detail_get_not_found_returns_404(self):
        r = self._staff_client().get(DETAIL_URL.format(99999999))
        assert r.status_code == 404

    def test_detail_get_returns_all_fields(self):
        r = self._staff_client().get(DETAIL_URL.format(self.bot.pk))
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == self.bot.id
        assert data["name"] == self.bot.name
        for field in (
            "prompt",
            "humanlike_delay",
            "reading_words_per_minute",
            "follow_up_on_idle",
            "idle_time_minutes",
            "follow_up_instruction_prompt",
        ):
            assert field in data, f"missing field: {field}"

    # ── PUT /api/bots/<pk>/ ────────────────────────────────────────────────────

    def test_detail_put_not_found_returns_404(self):
        r = self._put(self._staff_client(), 99999999, {"name": "x"})
        assert r.status_code == 404

    def test_detail_put_invalid_json_returns_400(self):
        r = self._staff_client().put(
            DETAIL_URL.format(self.bot.pk),
            data="bad-json",
            content_type="application/json",
        )
        assert r.status_code == 400

    def test_detail_put_updates_name_and_prompt(self):
        new_name = f"renamed_{uuid.uuid4().hex[:6]}"
        r = self._put(
            self._staff_client(),
            self.bot.pk,
            {"name": new_name, "prompt": "Updated prompt."},
        )
        assert r.status_code == 200
        self.bot.refresh_from_db()
        assert self.bot.name == new_name
        assert self.bot.prompt == "Updated prompt."

    # ── DELETE /api/bots/<pk>/ ─────────────────────────────────────────────────

    def test_detail_delete_not_found_returns_404(self):
        r = self._staff_client().delete(DETAIL_URL.format(99999999))
        assert r.status_code == 404

    def test_detail_delete_removes_bot_returns_204(self):
        temp = Bot.objects.create(
            name=f"del_{uuid.uuid4().hex[:6]}",
            prompt="Temp.",
            ai_model=self.model,
            model_type="OpenAI",
            model_id="gpt-4o-mini",
        )
        with patch("chatbot.services.bots.delete"):
            r = self._staff_client().delete(DETAIL_URL.format(temp.pk))
        assert r.status_code == 204
        assert not Bot.objects.filter(pk=temp.pk).exists()
