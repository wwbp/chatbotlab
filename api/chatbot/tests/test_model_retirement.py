"""
Retired models: what the bot admin and the bots API offer.

Many study bots point at models that no longer answer. The decision is to
freeze those bots, never change them: a retired model stays on the bot that
uses it (the record of an old study must not change), but is not offered for
new bots.

Coverage:
  - a new bot can only choose an active model from an active provider
  - a bot on a retired model keeps it: it is offered, labelled, and saved as is
  - a bot cannot be switched to a *different* retired model
  - the change page and the bot list show the retirement
  - the bots API refuses a retired model for a new bot
"""

import json
import uuid

import pytest
from django.contrib.auth.models import User
from django.test import Client

from chatbot.admin import BotAdminForm
from chatbot.models import Bot, Model, ModelProvider

ADMIN = "/api/admin/chatbot/bot/"


@pytest.fixture
def models(db):
    live = ModelProvider.objects.create(name="Live", display_name="Live")
    gone = ModelProvider.objects.create(
        name="Gone", display_name="Gone", is_active=False
    )
    return {
        "active": Model.objects.create(
            provider=live, model_id="active-1", display_name="Active One"
        ),
        "retired": Model.objects.create(
            provider=live,
            model_id="retired-1",
            display_name="Retired One",
            is_active=False,
        ),
        "retired_2": Model.objects.create(
            provider=live,
            model_id="retired-2",
            display_name="Retired Two",
            is_active=False,
        ),
        # An active model of an inactive provider is not offered either.
        "provider_off": Model.objects.create(
            provider=gone, model_id="orphan-1", display_name="Orphan One"
        ),
    }


@pytest.fixture
def frozen_bot(models):
    return Bot.objects.create(
        name=f"frozen_{uuid.uuid4().hex[:6]}",
        prompt="An old study's prompt.",
        ai_model=models["retired"],
    )


@pytest.fixture
def admin_client(db):
    user = User.objects.create_user(
        username=f"su_{uuid.uuid4().hex[:6]}",
        password="pass",
        is_staff=True,
        is_superuser=True,
    )
    client = Client()
    client.force_login(user)
    return client


def _choices(form):
    return set(form.fields["ai_model"].queryset)


def _change_post(client, bot, **changes):
    return _form_post(client, f"{ADMIN}{bot.pk}/change/", **changes)


def _form_post(client, url, **changes):
    """POST an admin form back with its current values + changes."""
    form = client.get(url).context["adminform"].form
    data = {}
    for name in form.fields:
        value = form[name].value()
        if value is None or value is False:
            continue
        data[name] = value
    data.update(changes)
    return client.post(url, data)


def _offered(form, models):
    """Which of the fixture's models the form offers (others may exist)."""
    return {k for k, m in models.items() if m in _choices(form)}


def test_new_bot_is_offered_only_active_models(models):
    form = BotAdminForm()
    assert _offered(form, models) == {"active"}
    assert all(not m.retired for m in _choices(form))


def test_bot_on_a_retired_model_is_offered_it_labelled(models, frozen_bot):
    form = BotAdminForm(instance=frozen_bot)
    assert _offered(form, models) == {"active", "retired"}
    labels = dict(
        (str(value), label) for value, label in form.fields["ai_model"].choices
    )
    assert labels[str(models["retired"].pk)].endswith("(retired)")
    assert not labels[str(models["active"].pk)].endswith("(retired)")


def test_saving_a_frozen_bot_keeps_its_retired_model(admin_client, frozen_bot):
    response = _change_post(admin_client, frozen_bot, prompt="Typo fixed.")
    assert response.status_code == 302
    frozen_bot.refresh_from_db()
    assert frozen_bot.prompt == "Typo fixed."
    assert frozen_bot.ai_model.model_id == "retired-1"


def test_a_bot_cannot_switch_to_another_retired_model(
    admin_client, models, frozen_bot
):
    response = _change_post(admin_client, frozen_bot, ai_model=models["retired_2"].pk)
    assert response.status_code == 200  # form redisplayed with an error
    assert "ai_model" in response.context["adminform"].form.errors
    frozen_bot.refresh_from_db()
    assert frozen_bot.ai_model == models["retired"]


def test_new_bot_cannot_take_a_retired_model(admin_client, models):
    response = _form_post(
        admin_client,
        f"{ADMIN}add/",
        name="new_bot",
        prompt="p",
        ai_model=models["retired"].pk,
    )
    assert response.status_code == 200
    assert "ai_model" in response.context["adminform"].form.errors
    assert not Bot.objects.filter(name="new_bot").exists()


def test_change_page_says_the_model_is_retired(admin_client, frozen_bot):
    html = admin_client.get(f"{ADMIN}{frozen_bot.pk}/change/").content.decode()
    assert "Model retired" in html


def test_bot_list_marks_retired_models(admin_client, models, frozen_bot):
    Bot.objects.create(name="live_bot", prompt="p", ai_model=models["active"])
    html = admin_client.get(ADMIN).content.decode()
    assert "Retired One (retired)" in html
    assert "Active One (retired)" not in html


def test_bots_api_refuses_a_retired_model(admin_client, models):
    response = admin_client.post(
        "/api/bots/",
        data=json.dumps(
            {"name": "api_bot", "model_type": "Live", "model_id": "retired-1"}
        ),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert not Bot.objects.filter(name="api_bot").exists()
