from django.apps import AppConfig

# A module import, not `from ... import AdminConfig`: Django picks an app's
# config from the AppConfig classes in its apps.py, and an imported
# AdminConfig (default = True) would compete with ChatbotConfig.
from django.contrib.admin import apps as admin_apps


class ChatbotConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "chatbot"

    def ready(self):
        from . import access_log  # noqa: F401 — connects the auth-signal receivers


class ChatbotLabAdminConfig(admin_apps.AdminConfig):
    """django.contrib.admin with our site class; listed in INSTALLED_APPS."""

    default = False  # only used where INSTALLED_APPS names it
    default_site = "chatbot.admin_site.ChatbotLabAdminSite"
