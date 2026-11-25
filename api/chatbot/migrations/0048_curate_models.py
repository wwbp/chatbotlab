from django.db import migrations
from django.db.models import Q

# Frozen copy of the list in chatbot/models.py at the time of this migration:
# a migration must not change meaning when that list changes later.
_CLAUDE = ["Chat", "Reasoning", "Code", "Analysis"]
CURATED_MODELS = {
    "Bedrock": [
        {
            "model_id": "us.anthropic.claude-haiku-5-5",
            "display_name": "Claude Haiku 5.5",
            "capabilities": ["Chat", "Basic Reasoning", "Code"],
        },
        {
            "model_id": "us.anthropic.claude-sonnet-4-6",
            "display_name": "Claude Sonnet 4.6",
            "capabilities": _CLAUDE,
        },
        {
            "model_id": "us.anthropic.claude-sonnet-5-5",
            "display_name": "Claude Sonnet 5.5",
            "capabilities": _CLAUDE,
        },
        {
            "model_id": "us.anthropic.claude-opus-4-7",
            "display_name": "Claude Opus 4.7",
            "capabilities": _CLAUDE,
        },
        {
            "model_id": "us.anthropic.claude-opus-5-5",
            "display_name": "Claude Opus 5.5",
            "capabilities": _CLAUDE,
        },
    ],
    "OpenAI": [
        {
            "model_id": "gpt-4o-mini",
            "display_name": "GPT-4o Mini",
            "capabilities": ["Chat", "Vision", "Audio", "Reasoning"],
        },
        {
            "model_id": "gpt-4.1",
            "display_name": "GPT-4.1",
            "capabilities": ["Chat", "Reasoning", "Analysis"],
        },
        {
            "model_id": "gpt-5-mini",
            "display_name": "GPT-5 Mini",
            "capabilities": ["Chat", "Reasoning", "Code", "Analysis"],
        },
    ],
}
PROVIDER_DEFAULTS = {
    "Bedrock": {"display_name": "Amazon Bedrock"},
    "OpenAI": {"display_name": "OpenAI"},
}


def curate_models(apps, schema_editor):
    """Offer only the curated models for new bots.

    Only is_active changes, and missing curated models are created. Nothing is
    deleted and no Bot is touched: bots on models that are now inactive keep
    them (frozen), so old studies' records stay exactly as they were.
    """
    Model = apps.get_model("chatbot", "Model")
    ModelProvider = apps.get_model("chatbot", "ModelProvider")

    curated = Q(pk__in=[])
    for provider_name, models in CURATED_MODELS.items():
        provider, _ = ModelProvider.objects.get_or_create(
            name=provider_name, defaults=PROVIDER_DEFAULTS[provider_name]
        )
        for data in models:
            Model.objects.get_or_create(
                provider=provider,
                model_id=data["model_id"],
                defaults={
                    "display_name": data["display_name"],
                    "capabilities": data["capabilities"],
                },
            )
            curated |= Q(provider=provider, model_id=data["model_id"])

    Model.objects.filter(curated).update(is_active=True)
    Model.objects.exclude(curated).update(is_active=False)
    ModelProvider.objects.filter(name__in=CURATED_MODELS).update(is_active=True)
    # Claude via Bedrock only: the lab's Anthropic API organisation is disabled.
    ModelProvider.objects.filter(name="Anthropic").update(is_active=False)


def reverse_curate_models(apps, schema_editor):
    """A deliberate no-op.

    Before this migration nothing read is_active (the admin offered every
    model), so the code you roll back to ignores the values this migration set:
    leaving them is behaviourally identical to restoring them. The models it
    created stay too, because rows are never deleted; old code just offers them
    alongside the rest.
    """


class Migration(migrations.Migration):
    dependencies = [
        ("chatbot", "0047_moderation_switch_name"),
    ]

    operations = [
        migrations.RunPython(curate_models, reverse_curate_models),
    ]
