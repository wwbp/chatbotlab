"""
Admin layout: the front page grouped by job, and the Bot admin.

The default admin index lists models alphabetically under their Django app
("Authentication and Authorization", "Chatbot"), which says nothing about
what a researcher came to do. chatbot/admin_site.py regroups the same,
permission-filtered models into Study setup / Participant data / Oversight;
the nav sidebar reads the same list.

The Bot list used to show 14 columns and fetch each bot's avatar (a query
and a presigned URL per row). It now shows what differs between the bots of a
study's cells, with a bounded number of queries.
"""

import uuid
from pathlib import Path

import pytest
from django.contrib.auth.models import Group, User
from django.contrib.staticfiles.finders import find
from django.test import Client

from chatbot.models import Avatar, Bot, Persona

INDEX = "/api/admin/"
BOT_LIST = "/api/admin/chatbot/bot/"

STUDY_SETUP = ["Bot", "Persona", "ModerationSettings", "SurveyIngestToken"]
PARTICIPANT_DATA = [
    "Conversation",
    "Utterance",
    "SurveyResponse",
    "Keystroke",
    "Avatar",
]
OVERSIGHT = ["AccessLog", "User", "Group"]


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


def _groups(app_list):
    return [
        (app["name"], [m["object_name"] for m in app["models"]]) for app in app_list
    ]


# ── Front page and nav sidebar ───────────────────────────────────────────────


@pytest.mark.django_db
def test_superuser_index_shows_three_groups_in_order():
    response = _client_for(superuser=True).get(INDEX)

    assert _groups(response.context["app_list"]) == [
        ("Study setup", STUDY_SETUP),
        ("Participant data", PARTICIPANT_DATA),
        ("Oversight", OVERSIGHT),
    ]
    html = response.content.decode()
    positions = [
        html.index(name) for name in ("Study setup", "Participant data", "Oversight")
    ]
    assert positions == sorted(positions)


@pytest.mark.django_db
def test_config_editor_sees_only_study_setup():
    response = _client_for("Config editor").get(INDEX)

    # Survey ingest tokens are superuser-only, so they drop out of the group.
    assert _groups(response.context["app_list"]) == [
        ("Study setup", ["Bot", "Persona", "ModerationSettings"]),
    ]


@pytest.mark.django_db
def test_data_viewer_sees_study_setup_and_participant_data_but_not_oversight():
    response = _client_for("Data viewer").get(INDEX)

    groups = _groups(response.context["app_list"])
    assert groups == [
        ("Study setup", ["Bot", "Persona", "ModerationSettings"]),
        ("Participant data", PARTICIPANT_DATA),
    ]
    study_setup = response.context["app_list"][0]["models"]
    assert all(m["view_only"] for m in study_setup)


@pytest.mark.django_db
def test_nav_sidebar_uses_the_same_grouping():
    response = _client_for(superuser=True).get(BOT_LIST)

    assert [app["name"] for app in response.context["available_apps"]] == [
        "Study setup",
        "Participant data",
        "Oversight",
    ]
    # A group spans apps, so no heading is "current"; the model row still is.
    html = response.content.decode()
    assert "current-app" not in html
    assert "current-model" in html


@pytest.mark.django_db
def test_app_index_page_still_works():
    response = _client_for(superuser=True).get("/api/admin/chatbot/")

    assert response.status_code == 200


@pytest.mark.django_db
def test_moderation_settings_is_called_the_moderation_switch():
    html = _client_for(superuser=True).get(INDEX).content.decode()

    assert "Moderation switch" in html
    assert "Global Moderation" not in html


# ── Bot admin ────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_bot_save_as_new_creates_a_copy(make_bot):
    persona = Persona.objects.create(name="warm", instructions="Be warm.")
    bot = make_bot(name="cell_a")
    bot.personas.add(persona)
    client = _client_for(superuser=True)
    url = f"{BOT_LIST}{bot.pk}/change/"
    page = client.get(url)
    assert 'name="_saveasnew"' in page.content.decode()
    form = page.context["adminform"].form
    data = {}
    for name in form.fields:
        value = form[name].value()
        if value is None or value is False:
            continue
        data[name] = value

    response = client.post(url, {**data, "name": "cell_b", "_saveasnew": "1"})

    assert response.status_code == 302
    copy = Bot.objects.get(name="cell_b")
    assert copy.pk != bot.pk
    assert copy.prompt == bot.prompt
    assert list(copy.personas.all()) == [persona]
    assert Bot.objects.filter(name="cell_a").exists()
    Bot.objects.filter(name="cell_b").delete()


@pytest.mark.django_db
def test_bot_changelist_query_count_is_bounded(make_bot, django_assert_max_num_queries):
    persona = Persona.objects.create(name="p", instructions="i")
    for _ in range(10):
        bot = make_bot(avatar_type="default")
        bot.personas.add(persona)
        Avatar.objects.create(bot=bot, chatbot_avatar="a.png")
    client = _client_for(superuser=True)

    # 10 today (session, user, counts, the page, filter choices); an N+1
    # (persona count, avatar, model) would add at least 10.
    with django_assert_max_num_queries(12):
        response = client.get(BOT_LIST)

    assert response.status_code == 200


@pytest.mark.django_db
def test_bot_changelist_shows_the_columns_that_differ_between_cells(make_bot):
    make_bot()
    response = _client_for(superuser=True).get(BOT_LIST)

    columns = list(response.context["cl"].list_display)
    assert columns == [
        "action_checkbox",
        "name",
        "model_name",
        "persona_count",
        "avatar_type",
        "survey_context",
        "updated",
    ]


@pytest.mark.django_db
def test_bot_form_uses_checkboxes_for_personas_and_short_textareas(make_bot):
    bot = make_bot()
    client = _client_for(superuser=True)
    form = client.get(f"{BOT_LIST}{bot.pk}/change/").context["adminform"].form

    assert form.fields["personas"].widget.widget.input_type == "checkbox"
    assert form.fields["initial_utterance"].widget.attrs["rows"] == 3
    assert form.fields["prompt"].widget.attrs.get("rows") != 3


# ── Status palette and table CSS ─────────────────────────────────────────────


@pytest.mark.django_db
def test_bot_list_uses_status_classes_not_hex_colours(make_bot):
    make_bot(survey_context_enabled=False)
    make_bot(survey_context_enabled=True, survey_context_preamble="")
    make_bot(survey_context_enabled=True, survey_context_preamble="Answers:")

    html = _client_for(superuser=True).get(BOT_LIST).content.decode()

    for cls in ("st-off", "st-warn", "st-ok"):
        assert f'class="{cls}"' in html
    assert 'style="color:#' not in html


@pytest.mark.django_db
def test_survey_token_page_uses_status_classes_not_hex_colours():
    from chatbot.models import SurveyIngestToken

    token = SurveyIngestToken.objects.create(note="study")
    html = (
        _client_for(superuser=True)
        .get(f"/api/admin/chatbot/surveyingesttoken/{token.pk}/change/")
        .content.decode()
    )

    assert "st-off" in html
    assert "color:#" not in html


def test_stylesheet_defines_sticky_header_and_status_classes():
    css = Path(find("admin/css/custom_admin.css")).read_text()

    assert "position: sticky" in css
    for cls in (".st-ok", ".st-warn", ".st-off", ".mono", ".num"):
        assert cls in css
