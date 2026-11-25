"""
Tests for the 0048 data migration that sets which models are offered, and for
the defaults a fresh install creates.

Prod had 33 Model rows, all "active" because nothing read the flag, and 31 of
68 bots on models that no longer answer. The migration narrows what is offered
for new bots to a short curated list, verified by a real call; it never deletes
a Model, ModelProvider or Bot and never changes a bot's model (those bots are
frozen and labelled instead).

Coverage:
  - exactly the curated models end up active; every other model inactive
  - curated models missing from the table are created; existing rows keep
    their display names
  - the Anthropic-direct provider is turned off; Claude is offered via Bedrock
  - no row deleted, no bot's model changed, counts before/after
  - running it twice changes nothing more; reverse changes nothing
  - get_or_create_default_models: a fresh install gets exactly the curated
    list, and an existing install gets nothing re-created or re-activated
"""

import importlib

import pytest
from django.apps import apps as global_apps

from chatbot.models import CURATED_MODELS, Bot, Model, ModelProvider

_migration = importlib.import_module("chatbot.migrations.0048_curate_models")
curate = _migration.curate_models
reverse = _migration.reverse_curate_models

CURATED = {
    ("Bedrock", "us.anthropic.claude-haiku-5-5"),
    ("Bedrock", "us.anthropic.claude-sonnet-4-6"),
    ("Bedrock", "us.anthropic.claude-sonnet-5-5"),
    ("Bedrock", "us.anthropic.claude-opus-4-7"),
    ("Bedrock", "us.anthropic.claude-opus-5-5"),
    ("OpenAI", "gpt-4o-mini"),
    ("OpenAI", "gpt-4.1"),
    ("OpenAI", "gpt-5-mini"),
}


def _active():
    return set(
        Model.objects.filter(is_active=True).values_list("provider__name", "model_id")
    )


def _provider(name):
    return ModelProvider.objects.get_or_create(
        name=name, defaults={"display_name": name}
    )[0]


@pytest.fixture
def prod_like(db):
    """The pre-migration world: every model active, some bots on dead models."""
    openai, anthropic, bedrock = (
        _provider(n) for n in ("OpenAI", "Anthropic", "Bedrock")
    )
    for provider, model_id, name in [
        (openai, "gpt-4", "GPT-4"),
        (openai, "gpt-4o-mini", "GPT-4o Mini"),
        (anthropic, "claude-sonnet-4-20250514", "Claude Sonnet 4"),
        (bedrock, "us.anthropic.claude-sonnet-4-20250514-v1:0", "Old Sonnet 4"),
        (bedrock, "us.anthropic.claude-sonnet-4-6", "Anthropic Claude Sonnet 4.6"),
    ]:
        Model.objects.update_or_create(
            provider=provider, model_id=model_id, defaults={"display_name": name}
        )
    # Curated models that prod doesn't have yet must be created by the migration.
    Model.objects.filter(
        model_id__in=["us.anthropic.claude-haiku-5-5", "us.anthropic.claude-opus-5-5"]
    ).delete()
    Model.objects.update(is_active=True)
    ModelProvider.objects.update(is_active=True)
    bots = [
        Bot.objects.create(
            name=f"frozen_{m}",
            prompt="old study",
            ai_model=Model.objects.get(model_id=m),
        )
        for m in (
            "gpt-4",
            "claude-sonnet-4-20250514",
            "us.anthropic.claude-sonnet-4-20250514-v1:0",
        )
    ]
    return bots


def test_only_the_curated_models_are_active(prod_like):
    curate(global_apps, None)
    assert _active() == CURATED


def test_missing_curated_models_are_created_existing_names_kept(prod_like):
    curate(global_apps, None)
    assert (
        Model.objects.get(model_id="us.anthropic.claude-haiku-5-5").display_name
        == "Claude Haiku 5.5"
    )
    assert (
        Model.objects.get(model_id="us.anthropic.claude-opus-5-5").provider.name
        == "Bedrock"
    )
    # An existing row is left as it was, apart from is_active.
    assert (
        Model.objects.get(model_id="us.anthropic.claude-sonnet-4-6").display_name
        == "Anthropic Claude Sonnet 4.6"
    )


def test_anthropic_direct_is_off(prod_like):
    curate(global_apps, None)
    assert not ModelProvider.objects.get(name="Anthropic").is_active
    assert ModelProvider.objects.get(name="Bedrock").is_active
    assert ModelProvider.objects.get(name="OpenAI").is_active


def test_nothing_deleted_and_no_bot_changed(prod_like):
    model_pks = set(Model.objects.values_list("pk", flat=True))
    provider_pks = set(ModelProvider.objects.values_list("pk", flat=True))
    bots_before = dict(Bot.objects.values_list("pk", "ai_model_id"))
    missing = len(
        CURATED - set(Model.objects.values_list("provider__name", "model_id"))
    )

    curate(global_apps, None)

    assert model_pks <= set(Model.objects.values_list("pk", flat=True))
    assert Model.objects.count() == len(model_pks) + missing
    assert set(ModelProvider.objects.values_list("pk", flat=True)) == provider_pks
    assert dict(Bot.objects.values_list("pk", "ai_model_id")) == bots_before
    # A frozen bot's model row is still there, now inactive.
    for bot in prod_like:
        bot.refresh_from_db()
        assert bot.ai_model.retired


def test_running_twice_changes_nothing_more(prod_like):
    curate(global_apps, None)
    snapshot = list(Model.objects.values_list("pk", "is_active", "display_name"))
    curate(global_apps, None)
    assert list(Model.objects.values_list("pk", "is_active", "display_name")) == (
        snapshot
    )


def test_reverse_changes_nothing(prod_like):
    curate(global_apps, None)
    snapshot = (
        list(Model.objects.values_list("pk", "is_active")),
        list(ModelProvider.objects.values_list("pk", "is_active")),
        list(Bot.objects.values_list("pk", "ai_model_id")),
    )
    reverse(global_apps, None)
    assert snapshot == (
        list(Model.objects.values_list("pk", "is_active")),
        list(ModelProvider.objects.values_list("pk", "is_active")),
        list(Bot.objects.values_list("pk", "ai_model_id")),
    )


def test_migration_and_defaults_agree_on_the_list():
    from_models = {(p, m["model_id"]) for p, ms in CURATED_MODELS.items() for m in ms}
    from_migration = {
        (p, m["model_id"]) for p, ms in _migration.CURATED_MODELS.items() for m in ms
    }
    assert from_models == from_migration == CURATED


@pytest.mark.django_db
def test_fresh_install_gets_exactly_the_curated_list():
    Model.objects.all().delete()
    Model.get_or_create_default_models()
    assert set(Model.objects.values_list("provider__name", "model_id")) == CURATED
    assert _active() == CURATED


@pytest.mark.django_db
def test_defaults_never_resurrect_or_reactivate(prod_like):
    curate(global_apps, None)
    Model.objects.filter(model_id="gpt-4.1").update(is_active=False)
    count = Model.objects.count()
    Model.get_or_create_default_models()
    assert Model.objects.count() == count
    assert not Model.objects.get(model_id="gpt-4.1").is_active
    assert not Model.objects.get(model_id="gpt-4").is_active
    assert not ModelProvider.objects.get(name="Anthropic").is_active
