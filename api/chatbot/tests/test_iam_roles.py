"""
Tests for the site-wide roles (Config editor, Data viewer) created by the 0043
data migration, and for the permission checks that make them mean something.

Accounts used to be one shared superuser, so every guard only asked "is this a
staff member?". With named staff accounts that is no longer enough. The roles
keep bot configuration and participant data apart (least privilege): a Config
editor edits bots but never sees participants; a Data viewer reads and exports
participant data but changes nothing. A person may hold both. Everything else
(accounts, survey ingest tokens, deletes) stays with superusers.

Coverage:
  - ROLE_PERMISSIONS (pure, no DB): the exact permission set of each role
  - the migration creates exactly those groups and its reverse removes them
  - the admin: who can open which changelist, change a bot, and export
  - bots/avatar/upload APIs check the matching model permission, not just is_staff
  - the custom "delete avatars" admin action needs the delete permission
"""

import importlib
import json
import uuid
from unittest.mock import patch

import pytest
from django.apps import apps as global_apps
from django.contrib.auth.models import Group, User
from django.test import Client

from chatbot.models import Avatar

_migration = importlib.import_module("chatbot.migrations.0043_iam_roles")
ROLE_PERMISSIONS = _migration.ROLE_PERMISSIONS
create_roles = _migration.create_roles
remove_roles = _migration.remove_roles

CONFIG_EDITOR = "Config editor"
DATA_VIEWER = "Data viewer"
ADMIN = "/api/admin/{}/"
EXPORT_URL = "/api/admin/chatbot/{}/export/"
PARTICIPANT_DATA = (
    "conversation",
    "utterance",
    "keystroke",
    "surveyresponse",
    "avatar",
)
CONFIG = ("bot", "persona", "moderationsettings")


# ── ROLE_PERMISSIONS (pure) ───────────────────────────────────────────────────


def test_role_names_are_exact():
    assert set(ROLE_PERMISSIONS) == {CONFIG_EDITOR, DATA_VIEWER}


def test_config_editor_views_adds_and_changes_config_only():
    assert ROLE_PERMISSIONS[CONFIG_EDITOR] == {
        f"{action}_{model}" for action in ("view", "add", "change") for model in CONFIG
    }


def test_data_viewer_views_participant_data_and_config_only():
    assert ROLE_PERMISSIONS[DATA_VIEWER] == {
        f"view_{model}" for model in PARTICIPANT_DATA + CONFIG
    }


# ── Migration ────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_migration_groups_have_exactly_the_mapped_permissions():
    for name, codenames in ROLE_PERMISSIONS.items():
        group = Group.objects.get(name=name)
        assert set(group.permissions.values_list("codename", flat=True)) == codenames


@pytest.mark.django_db
def test_reverse_removes_the_groups_and_forward_restores_them():
    remove_roles(global_apps, None)
    assert not Group.objects.filter(name__in=ROLE_PERMISSIONS).exists()

    create_roles(global_apps, None)
    assert Group.objects.filter(name__in=ROLE_PERMISSIONS).count() == 2


# ── Helpers ──────────────────────────────────────────────────────────────────


def _client_for(*group_names, superuser=False):
    user = User.objects.create_user(
        username=f"u_{uuid.uuid4().hex[:8]}",
        password="pass",
        is_staff=True,
        is_superuser=superuser,
    )
    user.groups.add(*Group.objects.filter(name__in=group_names))
    client = Client()
    client.force_login(user)
    return client


def _json(client, method, url, data):
    return getattr(client, method)(
        url, data=json.dumps(data), content_type="application/json"
    )


def _bot_change_post(client, bot, **changes):
    """POST the bot's admin change form back with its current values + changes."""
    url = ADMIN.format(f"chatbot/bot/{bot.pk}/change")
    form = client.get(url).context["adminform"].form
    data = {}
    for name in form.fields:
        value = form[name].value()
        if value is None or value is False:
            continue  # unticked checkboxes and empty fields are simply absent
        data[name] = value
    data.update(changes)
    return client.post(url, data)


# ── Admin ────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_config_editor_can_open_and_change_a_bot_in_the_admin(make_bot):
    bot = make_bot()
    c = _client_for(CONFIG_EDITOR)
    assert c.get(ADMIN.format(f"chatbot/bot/{bot.pk}/change")).status_code == 200
    assert _bot_change_post(c, bot, prompt="changed").status_code == 302
    bot.refresh_from_db()
    assert bot.prompt == "changed"


@pytest.mark.django_db
@pytest.mark.parametrize("model", PARTICIPANT_DATA)
def test_config_editor_cannot_see_participant_data(model):
    c = _client_for(CONFIG_EDITOR)
    assert c.get(ADMIN.format(f"chatbot/{model}")).status_code == 403


@pytest.mark.django_db
def test_config_editor_cannot_export_conversations():
    c = _client_for(CONFIG_EDITOR)
    assert c.get(EXPORT_URL.format("conversation")).status_code == 403


@pytest.mark.django_db
@pytest.mark.parametrize("model", PARTICIPANT_DATA + CONFIG)
def test_data_viewer_can_view(model):
    c = _client_for(DATA_VIEWER)
    assert c.get(ADMIN.format(f"chatbot/{model}")).status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize("model", ["conversation", "utterance"])
def test_data_viewer_can_export(model):
    assert _client_for(DATA_VIEWER).get(EXPORT_URL.format(model)).status_code == 200


@pytest.mark.django_db
def test_data_viewer_cannot_change_a_bot_in_the_admin(make_bot):
    bot = make_bot()
    c = _client_for(DATA_VIEWER)
    url = ADMIN.format(f"chatbot/bot/{bot.pk}/change")
    assert c.post(url, {"name": "x", "prompt": "changed"}).status_code == 403
    bot.refresh_from_db()
    assert bot.prompt != "changed"


@pytest.mark.django_db
@pytest.mark.parametrize("group", [CONFIG_EDITOR, DATA_VIEWER])
@pytest.mark.parametrize(
    "path", ["chatbot/surveyingesttoken", "auth/user", "auth/group"]
)
def test_no_role_can_see_tokens_or_accounts(group, path):
    assert _client_for(group).get(ADMIN.format(path)).status_code == 403


@pytest.mark.django_db
def test_staff_without_a_role_cannot_export():
    assert _client_for().get(EXPORT_URL.format("conversation")).status_code == 403


# ── Bots API ─────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_data_viewer_can_read_bots_but_not_write(make_bot):
    bot = make_bot()
    c = _client_for(DATA_VIEWER)
    assert c.get("/api/bots/").status_code == 200
    assert c.get(f"/api/bots/{bot.pk}/").status_code == 200
    assert _json(c, "post", "/api/bots/", {"name": "x"}).status_code == 403
    assert _json(c, "put", f"/api/bots/{bot.pk}/", {"name": "x"}).status_code == 403
    assert c.delete(f"/api/bots/{bot.pk}/").status_code == 403


@pytest.mark.django_db
def test_config_editor_can_create_and_update_bots_but_not_delete(make_bot):
    bot = make_bot()
    c = _client_for(CONFIG_EDITOR)
    assert c.get("/api/bots/").status_code == 200
    created = _json(
        c,
        "post",
        "/api/bots/",
        {
            "name": f"b_{uuid.uuid4().hex[:6]}",
            "model_type": "OpenAI",
            "model_id": "gpt-4o-mini",
        },
    )
    assert created.status_code == 201
    assert _json(c, "put", f"/api/bots/{bot.pk}/", {"name": "y"}).status_code == 200
    assert c.delete(f"/api/bots/{bot.pk}/").status_code == 403


@pytest.mark.django_db
def test_only_superuser_can_delete_bots(make_bot):
    bot = make_bot()
    c = _client_for(CONFIG_EDITOR, DATA_VIEWER)
    assert c.delete(f"/api/bots/{bot.pk}/").status_code == 403
    superuser = _client_for(superuser=True)
    assert superuser.delete(f"/api/bots/{bot.pk}/").status_code == 204


@pytest.mark.django_db
def test_staff_without_a_role_cannot_read_bots():
    assert _client_for().get("/api/bots/").status_code == 403


# ── Avatar + upload APIs ─────────────────────────────────────────────────────


@pytest.mark.django_db
def test_no_role_can_write_avatars_or_upload(make_bot):
    bot = make_bot()
    c = _client_for(CONFIG_EDITOR, DATA_VIEWER)
    assert c.post("/api/avatar/").status_code == 403
    assert c.post(f"/api/avatar/{bot.pk}/").status_code == 403
    assert c.delete(f"/api/avatar/{bot.pk}/").status_code == 403
    assert c.get("/api/avatar-upload/").status_code == 403


@pytest.mark.django_db
def test_avatar_list_needs_view_avatar():
    assert _client_for(CONFIG_EDITOR).get("/api/avatar/").status_code == 403
    assert _client_for(DATA_VIEWER).get("/api/avatar/").status_code == 200


@pytest.mark.django_db
def test_superuser_passes_avatar_and_upload_guards():
    """Past the guard, missing params give 400 — proof the guard let us in."""
    c = _client_for(superuser=True)
    assert c.get("/api/avatar/").status_code == 200
    assert c.get("/api/avatar-upload/").status_code == 400


# ── Avatar admin action ──────────────────────────────────────────────────────


@pytest.mark.django_db
def test_data_viewer_cannot_bulk_delete_avatars_from_admin(make_bot):
    avatar = Avatar.objects.create(bot=make_bot())
    with patch("chatbot.admin.delete"):
        _client_for(DATA_VIEWER).post(
            "/api/admin/chatbot/avatar/",
            {"action": "delete_avatars", "_selected_action": [avatar.pk]},
        )
    assert Avatar.objects.filter(pk=avatar.pk).exists()
