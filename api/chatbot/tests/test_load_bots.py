import pytest
from django.core.management import call_command

from chatbot.models import Bot


@pytest.mark.django_db
def test_load_bots_keeps_admin_edits(make_bot):
    # load_bots runs on every container start (entrypoint.sh); it must seed
    # missing bots, never overwrite a prompt a researcher edited in the admin.
    for name in ("Helpful Assistant", "Doctor"):
        make_bot(name=name, prompt="Edited in the admin.")
    call_command("load_bots")
    assert set(Bot.objects.values_list("prompt", flat=True)) == {"Edited in the admin."}


@pytest.mark.django_db
def test_load_bots_creates_missing_bots():
    call_command("load_bots")
    bot = Bot.objects.get(name="Helpful Assistant")
    assert bot.ai_model.model_id == "gpt-4o-mini"  # from config.json
