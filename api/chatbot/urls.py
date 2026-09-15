from django.conf import settings
from django.urls import path

from .services.avatar import AvatarAPIView, AvatarDetailAPIView
from .services.bots import BotDetailAPIView, ListBotsAPIView
from .services.conversation import InitializeConversationAPIView
from .services.followup import FollowupAPIView
from .services.keystroke import update_keystrokes
from .services.survey import survey_response
from .services.upload import get_presigned_url
from .services.voicechat import get_realtime_session, upload_voice_utterance
from .views import ChatbotAPIView, health_check, test_upload

urlpatterns = [
    path("health/", health_check, name="health_check"),
    path(
        "api/initialize_conversation/",
        InitializeConversationAPIView.as_view(),
        name="initialize_conversation",
    ),
    path("api/chatbot/", ChatbotAPIView.as_view(), name="chatbot_api"),
    path("api/followup/", FollowupAPIView.as_view(), name="followup_api"),
    path("api/bots/", ListBotsAPIView.as_view(), name="list_bots"),
    path("api/bots/<int:pk>/", BotDetailAPIView.as_view(), name="bot-detail"),
    path("api/update_keystrokes/", update_keystrokes, name="update_keystrokes"),
    path("api/survey_response/", survey_response, name="survey_response"),
    path("api/session/", get_realtime_session, name="get_realtime_session"),
    path(
        "api/upload_voice_utterance/",
        upload_voice_utterance,
        name="upload_voice_utterance",
    ),
    path("api/avatar/", AvatarAPIView.as_view(), name="avatar"),
    path(
        "api/avatar/<str:bot_name>/",
        AvatarDetailAPIView.as_view(),
        name="avatar-detail",
    ),
    path("api/avatar-upload/", get_presigned_url, name="avatar-upload"),
]

if settings.DEBUG:
    urlpatterns += [
        path("test-upload/", test_upload, name="test_upload"),
    ]
