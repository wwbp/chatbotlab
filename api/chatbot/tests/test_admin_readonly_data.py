"""
Participant data is read-only in the admin for everyone, superusers included.

These tables have no edit history, so an admin edit would change research data
with no trace. Superusers can still delete (a participant withdraws); real
corrections need a recorded management command, not the admin.
"""

import pytest
from django.contrib import admin
from django.contrib.auth.models import User
from django.test import RequestFactory

from chatbot.models import Conversation, Keystroke, SurveyResponse, Utterance

PARTICIPANT_DATA = (Conversation, Utterance, Keystroke, SurveyResponse)


@pytest.fixture
def superuser_request(db):
    request = RequestFactory().get("/api/admin/")
    request.user = User.objects.create_superuser("su", "su@upenn.edu", "x" * 16)
    return request


@pytest.mark.django_db
@pytest.mark.parametrize("model", PARTICIPANT_DATA, ids=lambda m: m.__name__)
def test_superuser_cannot_add_or_change_participant_data(model, superuser_request):
    model_admin = admin.site.get_model_admin(model)
    assert not model_admin.has_add_permission(superuser_request)
    assert not model_admin.has_change_permission(superuser_request)
    # Still viewable, and still deletable for withdrawals.
    assert model_admin.has_view_permission(superuser_request)
    assert model_admin.has_delete_permission(superuser_request)


@pytest.mark.django_db
def test_conversation_page_has_no_save_button(superuser_request, make_conversation):
    from django.test import Client

    conversation = make_conversation()
    client = Client()
    client.force_login(superuser_request.user)
    html = client.get(
        f"/api/admin/chatbot/conversation/{conversation.pk}/change/"
    ).content.decode()
    assert 'name="_save"' not in html
    assert 'name="conversation_id"' not in html  # shown as text, not an input
