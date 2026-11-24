"""
Tests for the Studies page and the one-zip-per-study export (chatbot.admin_studies),
and for the from-to date filter on the Conversation and Utterance lists.

There is no Study model: a study is every conversation sharing a free-text
Conversation.study_name. Before this page a researcher had to find the study in
a sidebar filter and export four tables one at a time.

Coverage:
  - the page: one row per study with conversation, per-condition and utterance
    counts and first/last dates, newest activity first; condition and date
    filters; a fixed number of queries however many studies there are
  - row links open the Conversation and Utterance lists filtered to that study,
    including awkward names (n/a, empty, null, slash, quotes, unicode, "=")
  - the zip: four CSVs made by the existing Resources, holding exactly that
    study's rows (keystrokes by conversation_id, survey responses by
    survey_id + participant_id), honouring condition and date range
  - permissions: Data viewer yes, Config editor 403, anonymous to login; only
    the tables the user may view are in the zip
  - the export is written to the access log with the study name
  - the front page lists Studies only for users who may open it
  - DateRangeFilter: inclusive from-to on both changelists
"""

import csv
import datetime as dt
import io
import json
import uuid
import zipfile

import pytest
from django.contrib.auth.models import Group, Permission, User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils.http import urlencode

from chatbot.models import (
    AccessLog,
    Conversation,
    Keystroke,
    SurveyResponse,
    Utterance,
)

STUDIES = "/api/admin/studies/"
EXPORT = "/api/admin/studies/export/"
UTC = dt.UTC


def _client_for(*group_names, perms=(), superuser=False):
    user = User.objects.create_user(
        username=f"u_{uuid.uuid4().hex[:8]}",
        password="pass",
        is_staff=True,
        is_superuser=superuser,
    )
    for name in group_names:
        user.groups.add(Group.objects.get(name=name))
    for codename in perms:
        user.user_permissions.add(Permission.objects.get(codename=codename))
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def viewer(db):
    return _client_for("Data viewer")


@pytest.fixture
def make_session(make_conversation, make_bot):
    """A conversation with a start date, n utterances and optional extras."""
    bot = make_bot()

    def _make(study, condition, day, utterances=1, keystrokes=False, survey=None):
        cid = f"R_{uuid.uuid4().hex[:8]}"
        conversation = make_conversation(
            bot=bot,
            conversation_id=cid,
            participant_id=f"p_{cid}",
            study_name=study,
            user_group=condition,
            survey_id=survey or "n/a",
        )
        # started_time is auto_now_add, so it can only be set by an update.
        Conversation.objects.filter(pk=conversation.pk).update(
            started_time=dt.datetime(2026, 3, day, 12, tzinfo=UTC)
        )
        for i in range(utterances):
            Utterance.objects.create(
                conversation=conversation, speaker_id="user", text=f"{cid} {i}"
            )
        if keystrokes:
            Keystroke.objects.create(
                conversation_id=cid,
                total_time_on_page=1,
                total_time_away_from_page=0,
                keystroke_count=5,
                timestamp=dt.datetime(2026, 3, day, 12, tzinfo=UTC),
            )
        if survey:
            SurveyResponse.objects.create(
                survey_id=survey,
                participant_id=conversation.participant_id,
                answers=[{"question": "q", "answer": cid}],
            )
        return conversation

    return _make


@pytest.fixture
def two_studies(make_session):
    """stress: 2 control + 1 treatment (days 1, 2, 10); sleep: 1 control (day 5)."""
    return {
        "stress_c1": make_session("stress", "control", 1, 2, True, "SV_1"),
        "stress_c2": make_session("stress", "control", 2, 1),
        "stress_t": make_session("stress", "treatment", 10, 3, True, "SV_1"),
        "sleep": make_session("sleep", "control", 5, 4, True, "SV_2"),
    }


def _export(client, **params):
    return client.post(EXPORT + "?" + urlencode(params))


def _zip_rows(response):
    """{filename: list of row dicts} for a zip response."""
    assert response.status_code == 200
    assert response["Content-Type"] == "application/zip"
    assert "attachment" in response["Content-Disposition"]
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    return {
        name: list(csv.DictReader(io.StringIO(archive.read(name).decode("utf-8-sig"))))
        for name in archive.namelist()
    }


# ── The page ─────────────────────────────────────────────────────────────────


def test_page_counts_per_study(viewer, two_studies):
    response = viewer.get(STUDIES)

    assert response.status_code == 200
    stress, sleep = response.context["studies"]  # most recent activity first
    assert stress["name"] == "stress"
    assert stress["conversations"] == 3
    assert stress["conditions"] == [("control", 2), ("treatment", 1)]
    assert stress["utterances"] == 6
    assert stress["first"].date() == dt.date(2026, 3, 1)
    assert stress["last"].date() == dt.date(2026, 3, 10)
    assert sleep["name"] == "sleep"
    assert sleep["conversations"] == 1
    assert sleep["conditions"] == [("control", 1)]
    assert sleep["utterances"] == 4
    assert sleep["first"] == sleep["last"]


def test_page_is_a_real_table_with_a_labelled_filter_form(viewer, two_studies):
    html = viewer.get(STUDIES).content.decode()

    assert '<th scope="col">' in html
    assert '<th scope="row">' in html
    for field in ("user_group", "started_time_from", "started_time_to"):
        assert f'<label for="id_{field}">' in html
    assert 'type="date"' in html


def test_page_filters_by_condition_and_date(viewer, two_studies):
    by_condition = viewer.get(STUDIES, {"user_group": "treatment"})
    by_date = viewer.get(
        STUDIES, {"started_time_from": "2026-03-02", "started_time_to": "2026-03-05"}
    )

    (stress,) = by_condition.context["studies"]
    assert (stress["conversations"], stress["utterances"]) == (1, 3)
    # Both ends inclusive: day 2 (stress) and day 5 (sleep) are in.
    assert [(s["name"], s["conversations"]) for s in by_date.context["studies"]] == [
        ("sleep", 1),
        ("stress", 1),
    ]


def test_page_rejects_a_bad_date_without_crashing(viewer, two_studies):
    response = viewer.get(STUDIES, {"started_time_from": "not-a-date"})

    assert response.status_code == 200
    assert response.context["form"].errors
    assert response.context["studies"] == []


def test_page_query_count_does_not_grow_with_studies(viewer, make_session):
    make_session("s0", "control", 1)
    with CaptureQueriesContext(connection) as few:
        viewer.get(STUDIES)
    for i in range(1, 12):
        make_session(f"s{i}", f"cond{i % 3}", 1)
    with CaptureQueriesContext(connection) as many:
        response = viewer.get(STUDIES)

    assert len(response.context["studies"]) == 12
    assert len(many) == len(few)


# None is stored by nothing today (the API defaults to "n/a") but the column
# is nullable, so old or hand-loaded rows may have it.
AWKWARD_NAMES = [
    "n/a",
    "",
    None,
    "a/b\\c",
    'it\'s "quoted" <b>&',
    "étude ☃ 研究",
    "=cmd|' /C calc'!A0",
    "+1 (pilot)",
    "50% off?x=1&y=2#frag",
]


@pytest.mark.parametrize("name", AWKWARD_NAMES)
def test_awkward_study_names_link_and_export(viewer, make_session, name):
    target = make_session(name, "control", 1, utterances=2)
    make_session("other", "control", 1)

    response = viewer.get(STUDIES)
    study = next(s for s in response.context["studies"] if s["name"] == name)

    # The links are in the page, escaped, and open lists of exactly this study.
    conversations = viewer.get(study["conversations_url"])
    utterances = viewer.get(study["utterances_url"])
    assert conversations.status_code == utterances.status_code == 200
    assert [c.pk for c in conversations.context["cl"].result_list] == [target.pk]
    assert utterances.context["cl"].result_count == 2
    if name and "<" in name:
        assert name not in response.content.decode()  # escaped, never raw

    # And the export form on the row downloads exactly this study.
    files = _zip_rows(viewer.post(study["export_url"]))
    assert [r["conversation_id"] for r in files["conversations.csv"]] == [
        target.conversation_id
    ]
    assert len(files["utterances.csv"]) == 2


# ── The export ───────────────────────────────────────────────────────────────


def test_zip_has_four_csvs_with_only_that_study(viewer, two_studies):
    response = _export(viewer, study_name="stress")
    files = _zip_rows(response)

    assert sorted(files) == [
        "conversations.csv",
        "keystrokes.csv",
        "survey_responses.csv",
        "utterances.csv",
    ]
    stress_ids = {
        two_studies[k].conversation_id for k in ("stress_c1", "stress_c2", "stress_t")
    }
    assert {r["conversation_id"] for r in files["conversations.csv"]} == stress_ids
    assert {r["study_name"] for r in files["conversations.csv"]} == {"stress"}
    assert len(files["utterances.csv"]) == 6
    assert {r["conversation_id"] for r in files["utterances.csv"]} == stress_ids
    assert {r["conversation_id"] for r in files["keystrokes.csv"]} == {
        two_studies["stress_c1"].conversation_id,
        two_studies["stress_t"].conversation_id,
    }
    assert {r["participant_id"] for r in files["survey_responses.csv"]} == {
        two_studies["stress_c1"].participant_id,
        two_studies["stress_t"].participant_id,
    }
    assert "stress" in response["Content-Disposition"]


def test_zip_files_match_the_per_table_exports(viewer, two_studies):
    """Same Resources, same BOM: a zip member is what the table export gives."""
    archive = zipfile.ZipFile(io.BytesIO(_export(viewer, study_name="stress").content))
    data = archive.read("utterances.csv")

    assert data.startswith(b"\xef\xbb\xbf")
    header = data.decode("utf-8-sig").splitlines()[0].split(",")
    assert header[:3] == ["conversation_id", "study_name", "condition"]
    assert "message_count" in archive.read("conversations.csv").decode("utf-8-sig")


def test_zip_honours_condition_and_date_range(viewer, two_studies):
    control = _zip_rows(_export(viewer, study_name="stress", user_group="control"))
    late = _zip_rows(
        _export(
            viewer,
            study_name="stress",
            started_time_from="2026-03-02",
            started_time_to="2026-03-10",
        )
    )

    assert {r["conversation_id"] for r in control["conversations.csv"]} == {
        two_studies["stress_c1"].conversation_id,
        two_studies["stress_c2"].conversation_id,
    }
    assert len(control["utterances.csv"]) == 3
    assert len(control["keystrokes.csv"]) == 1
    assert len(control["survey_responses.csv"]) == 1
    # Day 2 and day 10 are both inside an inclusive 2..10 range; day 1 is not.
    assert {r["conversation_id"] for r in late["conversations.csv"]} == {
        two_studies["stress_c2"].conversation_id,
        two_studies["stress_t"].conversation_id,
    }
    assert len(late["utterances.csv"]) == 4


def test_zip_for_a_study_without_keystrokes_or_surveys(viewer, make_session):
    make_session("bare", "control", 1)
    # Same survey, different participant: not this study's answer.
    SurveyResponse.objects.create(survey_id="n/a", participant_id="someone-else")
    Keystroke.objects.create(
        conversation_id="R_unregistered",
        total_time_on_page=1,
        total_time_away_from_page=0,
        keystroke_count=1,
        timestamp=dt.datetime(2026, 3, 1, tzinfo=UTC),
    )

    archive = zipfile.ZipFile(io.BytesIO(_export(viewer, study_name="bare").content))

    # Header only, so a script reading the zip finds every file it expects.
    assert archive.read("keystrokes.csv").decode("utf-8-sig").splitlines() == [
        "conversation_id,keystroke_count,total_time_on_page,total_time_away_from_page,timestamp"
    ]
    assert archive.read("survey_responses.csv").decode("utf-8-sig").splitlines() == [
        "survey_id,participant_id,received_at,answers"
    ]


def test_zip_drops_a_leading_equals_sign_in_every_file(viewer, make_conversation):
    """The zip goes through the same export code, so the same escaping.

    The "=" is dropped from IDs in every file alike, so they still join.
    """
    conversation = make_conversation(
        conversation_id="=R_1",
        participant_id="=p1",
        study_name="=study",
        user_group="+control",
        survey_id="=SV",
    )
    Utterance.objects.create(conversation=conversation, speaker_id="user", text="=1+1")
    Utterance.objects.create(conversation=conversation, speaker_id="user", text="@x -y")
    Keystroke.objects.create(
        conversation_id="=R_1",
        total_time_on_page=1,
        total_time_away_from_page=0,
        keystroke_count=5,
        timestamp=dt.datetime(2026, 3, 1, tzinfo=UTC),
    )
    SurveyResponse.objects.create(
        survey_id="=SV",
        participant_id="=p1",
        answers=[{"question": "q", "answer": "=1+1"}],
    )

    response = _export(viewer, study_name="=study")
    files = _zip_rows(response)

    (c,) = files["conversations.csv"]
    assert (c["conversation_id"], c["participant_id"], c["study_name"]) == (
        "R_1",
        "p1",
        "study",
    )
    assert c["condition"] == "+control"  # "+" is not escaped by the library
    assert {(u["conversation_id"], u["text"]) for u in files["utterances.csv"]} == {
        ("R_1", "1+1"),
        ("R_1", "@x -y"),
    }
    assert [k["conversation_id"] for k in files["keystrokes.csv"]] == ["R_1"]
    (s,) = files["survey_responses.csv"]
    assert (s["survey_id"], s["participant_id"]) == ("SV", "p1")
    # The answers cell is JSON and starts with "[", so its contents are as sent.
    assert json.loads(s["answers"]) == [{"question": "q", "answer": "=1+1"}]
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert all(archive.read(n).startswith(b"\xef\xbb\xbf") for n in archive.namelist())


def test_export_needs_post_a_study_and_valid_dates(viewer, two_studies):
    assert viewer.get(EXPORT + "?study_name=stress").status_code == 405
    assert viewer.post(EXPORT).status_code == 400
    assert _export(viewer, study_name="stress", started_time_to="x").status_code == 400
    assert _export(viewer, study_name="no such study").status_code == 404


def test_export_is_csrf_protected(two_studies):
    user = User.objects.create_superuser(username="root", password="pass")
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)

    assert client.post(EXPORT + "?study_name=stress").status_code == 403


def test_export_is_logged_with_the_study_name(viewer, two_studies):
    _export(viewer, study_name="stress", user_group="control")

    (entry,) = AccessLog.objects.filter(action=AccessLog.Action.EXPORT)
    assert entry.path == EXPORT
    assert entry.status_code == 200
    assert "study_name=stress" in entry.query
    assert "user_group=control" in entry.query


# ── Permissions ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_config_editor_gets_403(two_studies):
    client = _client_for("Config editor")

    assert client.get(STUDIES).status_code == 403
    assert _export(client, study_name="stress").status_code == 403


@pytest.mark.django_db
def test_anonymous_and_non_staff_are_sent_to_login(two_studies):
    participant = Client()
    participant.force_login(User.objects.create_user(username="p", password="pass"))

    for client in (Client(), participant):
        for response in (client.get(STUDIES), _export(client, study_name="stress")):
            assert response.status_code == 302
            assert response["Location"].startswith("/api/admin/login/")


@pytest.mark.django_db
def test_zip_holds_only_the_tables_the_user_may_view(two_studies):
    client = _client_for(perms=["view_conversation", "view_keystroke"])

    files = _zip_rows(_export(client, study_name="stress"))

    assert sorted(files) == ["conversations.csv", "keystrokes.csv"]


@pytest.mark.django_db
def test_front_page_lists_studies_only_for_those_who_may_open_it(two_studies):
    link = f'href="{STUDIES}"'

    assert link in _client_for("Data viewer").get("/api/admin/").content.decode()
    assert link in _client_for(superuser=True).get("/api/admin/").content.decode()
    assert link not in _client_for("Config editor").get("/api/admin/").content.decode()


# ── From-to date filter on the changelists ───────────────────────────────────


def test_conversation_list_filters_by_inclusive_date_range(viewer, two_studies):
    response = viewer.get(
        "/api/admin/chatbot/conversation/",
        {"started_time_from": "2026-03-02", "started_time_to": "2026-03-05"},
    )

    assert response.status_code == 200
    assert {c.pk for c in response.context["cl"].result_list} == {
        two_studies["stress_c2"].pk,
        two_studies["sleep"].pk,
    }
    html = response.content.decode()
    assert 'name="started_time_from" value="2026-03-02"' in html
    assert 'name="started_time_to" value="2026-03-05"' in html


def test_date_range_form_keeps_the_other_filters(viewer, two_studies):
    html = viewer.get(
        "/api/admin/chatbot/conversation/", {"study_name": "stress"}
    ).content.decode()

    # The search box has its own copy of the hidden inputs; look past it.
    sidebar = html.split('id="changelist-filter"')[1]
    assert '<input type="hidden" name="study_name" value="stress">' in sidebar


def test_date_range_filter_tolerates_blank_and_rejects_garbage(viewer, two_studies):
    url = "/api/admin/chatbot/conversation/"
    blank = viewer.get(url, {"started_time_from": "", "started_time_to": ""})
    garbage = viewer.get(url, {"started_time_from": "soon"})

    assert blank.context["cl"].result_count == 4
    assert garbage.status_code == 302  # the admin's own "?e=1" bad-filter redirect


def test_utterance_list_filters_by_date_range(viewer, two_studies):
    old = Utterance.objects.filter(conversation=two_studies["sleep"]).first()
    Utterance.objects.filter(pk=old.pk).update(
        created_time=dt.datetime(2026, 1, 15, 23, 59, tzinfo=UTC)
    )

    response = viewer.get(
        "/api/admin/chatbot/utterance/",
        {"created_time_from": "2026-01-15", "created_time_to": "2026-01-15"},
    )

    assert [u.pk for u in response.context["cl"].result_list] == [old.pk]
