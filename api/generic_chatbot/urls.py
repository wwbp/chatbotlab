"""
URL configuration for generic_chatbot project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from chatbot.staff_auth import StaffPasswordResetConfirmView, StaffPasswordResetView

admin.site.site_header = admin.site.site_title = "ChatbotLab"

# Password reset and invite links live outside /api/admin/ so the access log,
# which records admin paths, never stores a token. The admin-styled
# registration/* templates ship with django.contrib.admin.
_branding = {"extra_context": {"site_header": "ChatbotLab", "site_title": "ChatbotLab"}}

urlpatterns = [
    path("api/admin/", admin.site.urls),
    path(
        "api/auth/password_reset/",
        StaffPasswordResetView.as_view(**_branding),
        # This name makes the admin login show "Forgotten your password?".
        name="admin_password_reset",
    ),
    path(
        "api/auth/password_reset/done/",
        auth_views.PasswordResetDoneView.as_view(**_branding),
        name="password_reset_done",
    ),
    path(
        "api/auth/reset/<uidb64>/<token>/",
        StaffPasswordResetConfirmView.as_view(**_branding),
        name="password_reset_confirm",
    ),
    path(
        "api/auth/reset/done/",
        auth_views.PasswordResetCompleteView.as_view(**_branding),
        name="password_reset_complete",
    ),
    path("", include("chatbot.urls")),  # Include chatbot app URLs directly
]


if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
