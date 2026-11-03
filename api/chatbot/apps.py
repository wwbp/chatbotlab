from django.apps import AppConfig


class ChatbotConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "chatbot"

    def ready(self):
        from . import access_log  # noqa: F401 — connects the auth-signal receivers
