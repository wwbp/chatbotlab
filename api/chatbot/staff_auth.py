"""
Staff invites and self-service password reset.

Invites and resets share one mechanism: Django's single-use, 3-day
default_token_generator link to PasswordResetConfirmView under /api/auth/.
That prefix is outside /api/admin/, so the access log never stores a token.

Links are built from settings.SITE_URL, never from the request: behind
CloudFront -> ALB -> nginx, request.is_secure() is False and the Host may be
the Elastic Beanstalk hostname.
"""

import logging
from urllib.parse import urlparse

from django.conf import settings
from django.contrib.auth import views as auth_views
from django.contrib.auth.forms import PasswordResetForm
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import EmailMessage
from django.http import HttpResponseRedirect
from django.template import loader
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .access_log import Action, _write, client_ip
from .utils.rate_limit import within_rate_limit

logger = logging.getLogger(__name__)

# Per hour. Low per address (a person needs one or two); higher per IP because
# a lab can share one address.
RESETS_PER_EMAIL_PER_HOUR = 3
RESETS_PER_IP_PER_HOUR = 10

INVITE_TEMPLATES = (
    "registration/staff_invite_subject.txt",
    "registration/staff_invite_email.txt",
)
RESET_TEMPLATES = (
    "registration/staff_reset_subject.txt",
    "registration/staff_reset_email.txt",
)


def _domain():
    domain = urlparse(settings.SITE_URL).netloc
    if not domain:
        raise ImproperlyConfigured("SITE_URL is not set")
    return domain


class StaffPasswordResetForm(PasswordResetForm):
    def get_users(self, email):
        # Unlike Django's default, include users with an unusable password:
        # an invited person who lost the invite must be able to recover.
        return User.objects.filter(email__iexact=email, is_active=True, is_staff=True)

    def send_mail(
        self,
        subject_template_name,
        email_template_name,
        context,
        from_email,
        to_email,
        html_email_template_name=None,
    ):
        # Plain text only. Unlike Django's, this raises on failure so an invite
        # can report it; save() swallows it for anonymous resets.
        subject = "".join(
            loader.render_to_string(subject_template_name, context).splitlines()
        )
        body = loader.render_to_string(email_template_name, context)
        reply_to = context.get("reply_to")
        EmailMessage(
            subject,
            body,
            from_email,
            [to_email],
            reply_to=[reply_to] if reply_to else None,
        ).send()

    def save(self, **kwargs):
        try:
            kwargs.update(
                domain_override=_domain(),
                use_https=True,
                subject_template_name=RESET_TEMPLATES[0],
                email_template_name=RESET_TEMPLATES[1],
            )
            super().save(**kwargs)
        except Exception:
            # Never reveal to the requester whether the address exists.
            logger.exception("password reset email failed")


def send_set_password_email(user, inviter, invite=True):
    """Send `user` one set-password link: the invite text, or the reset text.

    Raises on failure (including SITE_URL unset); the caller reports it.
    """
    context = {
        "user": user,
        "domain": _domain(),
        "protocol": "https",
        "uid": urlsafe_base64_encode(force_bytes(user.pk)),
        "token": default_token_generator.make_token(user),
        "site_url": settings.SITE_URL.rstrip("/"),
    }
    templates = RESET_TEMPLATES
    if invite:
        templates = INVITE_TEMPLATES
        context.update(
            reply_to=inviter.email,
            inviter_name=inviter.get_full_name() or inviter.get_username(),
            roles=", ".join(sorted(user.groups.values_list("name", flat=True))),
        )
    StaffPasswordResetForm().send_mail(*templates, context, None, user.email)


class StaffPasswordResetView(auth_views.PasswordResetView):
    form_class = StaffPasswordResetForm

    def form_valid(self, form):
        email = form.cleaned_data["email"]
        _write(self.request, None, Action.PASSWORD_RESET_REQUESTED, username=email)
        # Count both, always. Over the limit: send nothing but show the same
        # page, so the limit reveals nothing about which addresses exist.
        email_ok = within_rate_limit(
            f"reset-email:{email.lower()}", RESETS_PER_EMAIL_PER_HOUR, 3600
        )
        ip_ok = within_rate_limit(
            f"reset-ip:{client_ip(self.request)}", RESETS_PER_IP_PER_HOUR, 3600
        )
        if not (email_ok and ip_ok):
            return HttpResponseRedirect(self.get_success_url())
        return super().form_valid(form)


class StaffPasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    def form_valid(self, form):
        response = super().form_valid(form)
        # The real path holds the uid; log a fixed one instead.
        _write(self.request, self.user, Action.PASSWORD_SET, path="/api/auth/reset/")
        return response
