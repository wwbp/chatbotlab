"""
The participant avatar flow (web/src/components/Avatar.js, /avatar-upload):

1. GET  /api/avatar-upload/  -> presigned S3 PUT URL for uploads/<file>
2. PUT  the photo to S3
3. POST /api/avatar/         -> generate the avatar, store it, delete the photo

Participants are not logged in, so steps 1 and 3 must work anonymously — but
only for bots whose avatar_type is "user", and only once per conversation,
because every avatar is a paid image generation.
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from django.core.files.base import ContentFile
from django.test import Client

from chatbot.models import Avatar

UPLOAD = "/api/avatar-upload/"
AVATAR = "/api/avatar/"


def _presign(client, bot_name, filename="p1_R_abc.jpeg"):
    query = {"filename": filename, "content_type": "image/jpeg"}
    if bot_name is not None:
        query["bot_name"] = bot_name
    with patch("chatbot.services.upload.boto3.client") as s3:
        s3.return_value.generate_presigned_url.return_value = "https://s3/put"
        return client.get(UPLOAD, query)


def _create(client, bot_name, conversation_id="R_abc", image_path="p1_R_abc.jpeg"):
    body = {
        "bot_name": bot_name,
        "conversation_id": conversation_id,
        "participant_id": "p1",
        "image_path": image_path,
    }
    return client.post(AVATAR, json.dumps(body), content_type="application/json")


@pytest.fixture
def s3():
    """Patch the S3 helpers avatar.py uses; yields the mocks."""
    with (
        patch("chatbot.services.avatar.download", return_value=MagicMock()) as dl,
        patch("chatbot.services.avatar.upload", return_value="avatar/x.png") as up,
        patch("chatbot.services.avatar.delete") as rm,
    ):
        yield {"download": dl, "upload": up, "delete": rm}


def _generated(name="p1_R_abc_bot_user_1_avatar.png"):
    image = ContentFile(b"png-bytes")
    image.name = name
    return patch("chatbot.services.avatar.generate_avatar", return_value=image)


# ── Step 1: presigned upload URL ──────────────────────────────────────────────


@pytest.mark.django_db
def test_participant_gets_upload_url_for_user_avatar_bot(make_bot):
    bot = make_bot(avatar_type="user")
    r = _presign(Client(), bot.name)
    assert r.status_code == 200
    assert r.json()["s3_url"] == "https://s3/put"


@pytest.mark.django_db
@pytest.mark.parametrize("avatar_type", ["default", "none"])
def test_anonymous_upload_url_refused_for_other_avatar_types(make_bot, avatar_type):
    bot = make_bot(avatar_type=avatar_type)
    assert _presign(Client(), bot.name).status_code == 403


@pytest.mark.django_db
def test_anonymous_upload_url_refused_without_or_unknown_bot():
    assert _presign(Client(), None).status_code == 403
    assert _presign(Client(), "no-such-bot").status_code == 403


# ── Step 3: create the participant's avatar ───────────────────────────────────


@pytest.mark.django_db
def test_participant_creates_avatar_and_photo_is_deleted(make_bot, s3):
    bot = make_bot(avatar_type="user")
    with _generated():
        r = _create(Client(), bot.name)
    assert r.status_code == 200
    avatar = Avatar.objects.get(bot=bot, bot_conversation="R_abc")
    assert avatar.participant_avatar == "p1_R_abc_bot_user_1_avatar.png"
    # The raw photo of the participant's face must not outlive its use.
    s3["delete"].assert_called_once_with("uploads", "p1_R_abc.jpeg")


@pytest.mark.django_db
def test_second_avatar_for_same_conversation_is_refused(make_bot, s3):
    bot = make_bot(avatar_type="user")
    Avatar.objects.create(bot=bot, bot_conversation="R_abc", participant_avatar="a")
    with _generated() as gen:
        r = _create(Client(), bot.name)
    assert r.status_code == 409
    gen.assert_not_called()  # no second paid generation
    assert Avatar.objects.filter(bot=bot, bot_conversation="R_abc").count() == 1


@pytest.mark.django_db
def test_failed_generation_saves_no_avatar(make_bot, s3):
    bot = make_bot(avatar_type="user")
    with patch("chatbot.services.avatar.openai.OpenAI") as openai_client:
        openai_client.return_value.images.edit.side_effect = RuntimeError("down")
        with patch("chatbot.services.avatar.os.getenv", return_value="set"):
            r = _create(Client(), bot.name)
    assert r.status_code == 502
    assert not Avatar.objects.filter(bot=bot).exists()


@pytest.mark.django_db
def test_anonymous_cannot_set_a_bots_default_avatar(make_bot, s3):
    bot = make_bot(avatar_type="default")
    with _generated() as gen:
        r = _create(Client(), bot.name, conversation_id=None)
    assert r.status_code == 403
    gen.assert_not_called()


@pytest.mark.django_db
def test_participant_avatar_needs_a_conversation_id(make_bot, s3):
    bot = make_bot(avatar_type="user")
    with _generated():
        assert _create(Client(), bot.name, conversation_id="").status_code == 400


@pytest.mark.django_db
def test_participant_image_path_cannot_escape_uploads(make_bot, s3):
    bot = make_bot(avatar_type="user")
    with _generated():
        r = _create(Client(), bot.name, image_path="../avatar/someone_else.png")
    assert r.status_code == 400
    s3["download"].assert_not_called()


# ── The chat page shows it ────────────────────────────────────────────────────


@pytest.mark.django_db
def test_chat_page_gets_the_participants_avatar(make_bot):
    bot = make_bot(avatar_type="user")
    Avatar.objects.create(bot=bot, bot_conversation="R_abc", participant_avatar="a.png")
    # Staging and production serve avatars from S3, not local media.
    with (
        patch.dict(os.environ, {"BACKEND_ENVIRONMENT": "production"}),
        patch("chatbot.services.avatar.get_presigned_url", return_value="https://s3/a"),
    ):
        r = Client().get(f"/api/avatar/{bot.name}/", {"conversation_id": "R_abc"})
    assert r.status_code == 200
    assert r.json()["image_url"] == "https://s3/a"
