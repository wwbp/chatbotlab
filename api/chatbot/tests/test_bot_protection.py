"""
Bots define study conditions, and conversations name their bot by
`Conversation.bot_name` (a string, not a foreign key). Deleting or renaming a
bot with conversations would leave a study's data pointing at nothing.

Owner's rule: a bot may be deleted only by a superuser, and only if it has no
conversations. Nobody may rename a bot that has conversations.

Coverage:
  - admin: change-page delete and bulk "delete selected", for a Config editor
    and a superuser, on an unused bot and a bot with a conversation
"""

import json
import uuid
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group, Permission, User
from django.core.exceptions import ValidationError
from django.db.models import ProtectedError
from django.test import Client

from chatbot.models import Bot
from chatbot.tests.test_iam_roles import _bot_change_post

ADMIN_BOTS = "/api/admin/chatbot/bot/"


def _client(*groups, superuser=False):
    user = User.objects.create_user(
        username=f"u_{uuid.uuid4().hex[:8]}",
        password="pass",
        is_staff=True,
        is_superuser=superuser,
    )
    user.groups.add(*Group.objects.filter(name__in=groups))
    client = Client()
    client.force_login(user)
    return client


def _bulk_delete(client, *bots):
    with patch("chatbot.admin.delete"):
        return client.post(
            ADMIN_BOTS,
            {
                "action": "delete_selected",
                "_selected_action": [b.pk for b in bots],
                "post": "yes",
            },
        )


def _page_delete(client, bot):
    with patch("chatbot.admin.delete"):
        return client.post(f"{ADMIN_BOTS}{bot.pk}/delete/", {"post": "yes"})


# ── Admin ────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_config_editor_cannot_delete_a_bot_from_its_page(make_bot):
    bot = make_bot()
    assert _page_delete(_client("Config editor"), bot).status_code == 403
    assert Bot.objects.filter(pk=bot.pk).exists()


@pytest.mark.django_db
def test_config_editor_cannot_bulk_delete_bots(make_bot):
    bot = make_bot()
    c = _client("Config editor")
    assert b'value="delete_selected"' not in c.get(ADMIN_BOTS).content
    _bulk_delete(c, bot)
    assert Bot.objects.filter(pk=bot.pk).exists()


@pytest.mark.django_db
def test_superuser_can_delete_an_unused_bot_from_its_page(make_bot):
    bot = make_bot()
    assert _page_delete(_client(superuser=True), bot).status_code == 302
    assert not Bot.objects.filter(pk=bot.pk).exists()


@pytest.mark.django_db
def test_superuser_can_bulk_delete_unused_bots(make_bot):
    bot = make_bot()
    _bulk_delete(_client(superuser=True), bot)
    assert not Bot.objects.filter(pk=bot.pk).exists()


@pytest.mark.django_db
def test_superuser_cannot_delete_a_bot_with_a_conversation_from_its_page(
    make_conversation,
):
    conv = make_conversation()
    bot = Bot.objects.get(name=conv.bot_name)
    c = _client(superuser=True)
    assert c.get(f"{ADMIN_BOTS}{bot.pk}/delete/").status_code == 403
    assert _page_delete(c, bot).status_code == 403
    assert Bot.objects.filter(pk=bot.pk).exists()


@pytest.mark.django_db
def test_superuser_bulk_delete_refuses_all_if_any_bot_has_a_conversation(
    make_bot, make_conversation
):
    unused = make_bot()
    used = make_bot()
    make_conversation(bot=used)
    r = _bulk_delete(_client(superuser=True), unused, used)
    assert Bot.objects.filter(pk=used.pk).exists()
    assert Bot.objects.filter(pk=unused.pk).exists()
    assert f"{used.name}: This bot has 1 conversation" in r.content.decode()


# ── API: DELETE /api/bots/<pk>/ ──────────────────────────────────────────────


def _api_delete(client, bot):
    with patch("chatbot.services.bots.delete"):
        return client.delete(f"/api/bots/{bot.pk}/")


@pytest.mark.django_db
def test_api_delete_forbidden_for_staff_with_delete_permission(make_bot):
    """Only superusers: even a staff user granted delete_bot gets 403."""
    bot = make_bot()
    user = User.objects.create_user(
        username=f"u_{uuid.uuid4().hex[:8]}", password="pass", is_staff=True
    )
    user.user_permissions.add(
        *Permission.objects.filter(codename__in=["delete_bot", "view_bot"])
    )
    c = Client()
    c.force_login(user)
    assert _api_delete(c, bot).status_code == 403
    assert Bot.objects.filter(pk=bot.pk).exists()


@pytest.mark.django_db
def test_api_delete_forbidden_for_config_editor(make_bot):
    bot = make_bot()
    assert _api_delete(_client("Config editor"), bot).status_code == 403
    assert Bot.objects.filter(pk=bot.pk).exists()


@pytest.mark.django_db
def test_api_delete_of_a_bot_with_a_conversation_is_409(make_bot, make_conversation):
    bot = make_bot()
    make_conversation(bot=bot)
    r = _api_delete(_client(superuser=True), bot)
    assert r.status_code == 409
    assert r.json()["error"] == Bot.in_use_message(1)
    assert Bot.objects.filter(pk=bot.pk).exists()


@pytest.mark.django_db
def test_api_delete_of_an_unused_bot_is_204(make_bot):
    bot = make_bot()
    assert _api_delete(_client(superuser=True), bot).status_code == 204
    assert not Bot.objects.filter(pk=bot.pk).exists()


# ── Model: last line of defence ──────────────────────────────────────────────


@pytest.mark.django_db
def test_bot_delete_raises_when_it_has_conversations(make_bot, make_conversation):
    bot = make_bot()
    make_conversation(bot=bot)
    with pytest.raises(ProtectedError, match="can't be deleted"):
        bot.delete()
    assert Bot.objects.filter(pk=bot.pk).exists()


@pytest.mark.django_db
def test_queryset_delete_raises_when_any_bot_has_conversations(
    make_bot, make_conversation
):
    unused = make_bot()
    used = make_bot()
    make_conversation(bot=used)
    with pytest.raises(ProtectedError, match="can't be deleted"):
        Bot.objects.filter(pk__in=[unused.pk, used.pk]).delete()
    assert Bot.objects.filter(pk__in=[unused.pk, used.pk]).count() == 2


@pytest.mark.django_db
def test_unused_bots_can_still_be_deleted(make_bot):
    one, two = make_bot(), make_bot()
    Bot.objects.get(pk=one.pk).delete()  # a copy: make_bot tears down `one`
    Bot.objects.filter(pk=two.pk).delete()
    assert not Bot.objects.filter(pk__in=[one.pk, two.pk]).exists()


# ── Rename: conversations name their bot, so a rename detaches them ──────────


@pytest.mark.django_db
def test_renaming_a_bot_with_conversations_raises(make_bot, make_conversation):
    bot = make_bot()
    make_conversation(bot=bot)
    old = bot.name
    bot.name = f"renamed_{uuid.uuid4().hex[:6]}"
    with pytest.raises(ValidationError, match="can't be deleted or renamed"):
        bot.clean()
    with pytest.raises(ValidationError, match="can't be deleted or renamed"):
        bot.save()
    assert Bot.objects.get(pk=bot.pk).name == old


@pytest.mark.django_db
def test_renaming_an_unused_bot_is_allowed(make_bot):
    bot = make_bot()
    bot.name = f"renamed_{uuid.uuid4().hex[:6]}"
    bot.clean()
    bot.save()
    assert Bot.objects.get(pk=bot.pk).name == bot.name


@pytest.mark.django_db
def test_other_edits_to_a_bot_with_conversations_still_save(
    make_bot, make_conversation
):
    bot = make_bot()
    make_conversation(bot=bot)
    bot.prompt = "New prompt."
    bot.save()
    assert Bot.objects.get(pk=bot.pk).prompt == "New prompt."


@pytest.mark.django_db
def test_admin_form_refuses_renaming_a_bot_with_conversations(
    make_bot, make_conversation
):
    bot = make_bot()
    make_conversation(bot=bot)
    r = _bot_change_post(_client("Config editor"), bot, name="renamed")
    assert r.status_code == 200  # the form, redisplayed with the error
    assert "can&#x27;t be deleted or renamed" in r.content.decode()
    assert Bot.objects.get(pk=bot.pk).name == bot.name


@pytest.mark.django_db
def test_admin_form_renames_an_unused_bot(make_bot):
    bot = make_bot()
    new = f"renamed_{uuid.uuid4().hex[:6]}"
    r = _bot_change_post(_client("Config editor"), bot, name=new)
    assert r.status_code == 302
    assert Bot.objects.get(pk=bot.pk).name == new


@pytest.mark.django_db
def test_api_put_refuses_renaming_a_bot_with_conversations(
    make_bot, make_conversation
):
    bot = make_bot()
    make_conversation(bot=bot)
    r = _client("Config editor").put(
        f"/api/bots/{bot.pk}/",
        data=json.dumps({"name": "renamed"}),
        content_type="application/json",
    )
    assert r.status_code == 409
    assert r.json()["error"] == Bot.in_use_message(1)
    assert Bot.objects.get(pk=bot.pk).name == bot.name
