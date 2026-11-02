"""
Tests for the site-wide roles (Researcher, Bot editor) created by the 0043 data
migration, and for the permission checks that make them mean something.

Accounts used to be one shared superuser, so every guard only asked "is this a
staff member?". With named staff accounts that is no longer enough: a Researcher
is staff (they need the admin to read and export data) but must not change bots.

Coverage:
  - ROLE_PERMISSIONS shape (pure, no DB): Researcher is read-only, Bot editor is
    a superset, neither touches tokens or accounts
  - the migration creates exactly those groups and its reverse removes them
  - admin export needs the model's view permission, which Researcher has
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

RESEARCHER = "Researcher"
BOT_EDITOR = "Bot editor"
EXPORT_URL = "/api/admin/chatbot/{}/export/"


# ── ROLE_PERMISSIONS (pure) ───────────────────────────────────────────────────


def test_researcher_can_only_view_participant_data_and_bots():
    assert ROLE_PERMISSIONS[RESEARCHER] == {
        f"view_{model}"
        for model in (
            "conversation",
            "utterance",
            "keystroke",
            "surveyresponse",
            "bot",
            "persona",
        )
    }


def test_bot_editor_is_researcher_plus_bot_config_edits():
    extra = ROLE_PERMISSIONS[BOT_EDITOR] - ROLE_PERMISSIONS[RESEARCHER]
    assert ROLE_PERMISSIONS[RESEARCHER] <= ROLE_PERMISSIONS[BOT_EDITOR]
    assert {p for p in extra if not p.startswith("view_")} == {
        f"{action}_{model}"
        for action in ("add", "change")
        for model in ("bot", "persona", "avatar", "moderationsettings")
    }


def test_no_role_can_delete_or_touch_tokens_or_accounts():
    for perms in ROLE_PERMISSIONS.values():
        assert not any(p.startswith("delete_") for p in perms)
        assert not any(
            p.endswith(("_surveyingesttoken", "_user", "_group")) for p in perms
        )


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


def _client_for(group_name=None):
    user = User.objects.create_user(
        username=f"u_{uuid.uuid4().hex[:8]}", password="pass", is_staff=True
    )
    if group_name:
        user.groups.add(Group.objects.get(name=group_name))
    client = Client()
    client.force_login(user)
    return client


def _json(client, method, url, data):
    return getattr(client, method)(
        url, data=json.dumps(data), content_type="application/json"
    )


# ── Admin export ─────────────────────────────────────────────────────────────


@pytest.mark.django_db
@pytest.mark.parametrize("model", ["conversation", "utterance", "bot"])
def test_researcher_can_export(model):
    assert _client_for(RESEARCHER).get(EXPORT_URL.format(model)).status_code == 200


@pytest.mark.django_db
def test_staff_without_a_role_cannot_export():
    assert _client_for().get(EXPORT_URL.format("conversation")).status_code == 403


# ── Bots API ─────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_researcher_can_read_bots_but_not_write(make_bot):
    bot = make_bot()
    c = _client_for(RESEARCHER)
    assert c.get("/api/bots/").status_code == 200
    assert c.get(f"/api/bots/{bot.pk}/").status_code == 200
    assert _json(c, "post", "/api/bots/", {"name": "x"}).status_code == 403
    assert _json(c, "put", f"/api/bots/{bot.pk}/", {"name": "x"}).status_code == 403
    assert c.delete(f"/api/bots/{bot.pk}/").status_code == 403


@pytest.mark.django_db
def test_bot_editor_can_create_and_update_bots_but_not_delete(make_bot):
    bot = make_bot()
    c = _client_for(BOT_EDITOR)
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
def test_staff_without_a_role_cannot_read_bots():
    assert _client_for().get("/api/bots/").status_code == 403


# ── Avatar + upload APIs ─────────────────────────────────────────────────────


@pytest.mark.django_db
def test_researcher_cannot_use_avatar_or_upload_endpoints(make_bot):
    bot = make_bot()
    c = _client_for(RESEARCHER)
    assert c.get("/api/avatar/").status_code == 403
    assert c.post("/api/avatar/").status_code == 403
    assert c.post(f"/api/avatar/{bot.pk}/").status_code == 403
    assert c.delete(f"/api/avatar/{bot.pk}/").status_code == 403
    assert c.get("/api/avatar-upload/").status_code == 403


@pytest.mark.django_db
def test_bot_editor_passes_avatar_and_upload_guards():
    """Past the guard, missing params give 400 — proof the guard let us in."""
    c = _client_for(BOT_EDITOR)
    assert c.get("/api/avatar/").status_code == 200
    assert c.get("/api/avatar-upload/").status_code == 400


# ── Avatar admin action ──────────────────────────────────────────────────────


@pytest.mark.django_db
def test_bot_editor_cannot_bulk_delete_avatars_from_admin(make_bot):
    avatar = Avatar.objects.create(bot=make_bot())
    with patch("chatbot.admin.delete"):
        _client_for(BOT_EDITOR).post(
            "/api/admin/chatbot/avatar/",
            {"action": "delete_avatars", "_selected_action": [avatar.pk]},
        )
    assert Avatar.objects.filter(pk=avatar.pk).exists()
