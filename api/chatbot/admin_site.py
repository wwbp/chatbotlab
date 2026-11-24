"""
The admin site: Django's, with the front page grouped by job instead of app.

Wired in by chatbot.apps.ChatbotLabAdminConfig (in INSTALLED_APPS in place of
django.contrib.admin), so `admin.site`, `@admin.register` and the login form
set in chatbot/admin.py all use this class. It lives in its own module
because the default site is imported before chatbot/admin.py runs.
"""

from django.contrib import admin
from django.urls import path, reverse

# (heading, slug, models as "app_label.ModelName"), in display order.
GROUPS = (
    (
        "Study setup",
        "study-setup",
        (
            "chatbot.Bot",
            "chatbot.Persona",
            "chatbot.ModerationSettings",
            "chatbot.SurveyIngestToken",
        ),
    ),
    (
        "Participant data",
        "participant-data",
        (
            "chatbot.Studies",  # a page, not a model: see get_app_list
            "chatbot.Conversation",
            "chatbot.Utterance",
            "chatbot.SurveyResponse",
            "chatbot.Keystroke",
            "chatbot.Avatar",
        ),
    ),
    ("Oversight", "oversight", ("chatbot.AccessLog", "auth.User", "auth.Group")),
)


class ChatbotLabAdminSite(admin.AdminSite):
    def get_urls(self):
        # Imported here: it needs the models, and this module loads before
        # the app registry is ready.
        from . import admin_studies

        # Ahead of Django's own patterns, which end in a catch-all.
        return [
            path(
                "studies/", self.admin_view(admin_studies.studies_view), name="studies"
            ),
            path(
                "studies/export/",
                self.admin_view(admin_studies.studies_export_view),
                name="studies_export",
            ),
            *super().get_urls(),
        ]

    def get_app_list(self, request, app_label=None):
        """Regroup Django's permission-filtered app list (index and sidebar).

        Only models the user may see are in _build_app_dict, so a group the
        user can see nothing in is dropped. An app's own page
        (/api/admin/chatbot/) keeps Django's per-app list.
        """
        if app_label:
            return super().get_app_list(request, app_label)
        models = {
            f"{app['app_label']}.{m['object_name']}": m
            for app in self._build_app_dict(request).values()
            for m in app["models"]
        }
        # The Studies page summarises conversations, so it is listed for
        # whoever may view those (the same check studies_view makes).
        if request.user.has_perm("chatbot.view_conversation"):
            models["chatbot.Studies"] = {
                "name": "Studies",
                "object_name": "Studies",
                "admin_url": reverse("admin:studies", current_app=self.name),
                "add_url": None,
                "view_only": True,
            }
        groups = [
            (name, slug, [models.pop(label, None) for label in labels])
            for name, slug, labels in GROUPS
        ]
        # A model registered later but not placed in a group still shows.
        groups.append(
            ("Other", "other", sorted(models.values(), key=lambda m: m["name"]))
        )
        return [
            {
                "name": name,
                "app_label": slug,
                # The template marks a group "current" when app_url is in the
                # path; a fragment never is, and the link leads back here.
                "app_url": f"{reverse('admin:index', current_app=self.name)}#{slug}",
                "has_module_perms": True,
                "models": [m for m in group if m],
            }
            for name, slug, group in groups
            if any(group)
        ]
