"""
Access log: who viewed, changed, or exported what in the admin.

Django's admin LogEntry records add/change/delete only, so it cannot answer
"who looked at or downloaded participant data". This writes one AccessLog row
per admin or staff-only API request (AccessLogMiddleware) and per login, logout
and failed login (auth signals).

Deliberately never recorded: request or response bodies, passwords, tokens, and
participant traffic (chat, keystrokes, survey ingest) — that last one is
high-volume and is not staff activity.
"""

import ipaddress
import logging

from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.contrib.auth import get_user_model
from django.contrib.auth.signals import (
    user_logged_in,
    user_logged_out,
    user_login_failed,
)
from django.db import transaction
from django.dispatch import receiver
from django.http import HttpRequest

from .models import AccessLog

logger = logging.getLogger(__name__)

Action = AccessLog.Action

ADMIN = "/api/admin/"
# Login/logout pages are recorded by the auth signals below, with better detail.
AUTH_PAGES = (ADMIN + "login/", ADMIN + "logout/")
# Endpoints guarded by require_perm (utils/auth.py) in services/.
STAFF_API = ("/api/bots/", "/api/avatar/", "/api/avatar-upload/")

# CloudFront fronts /api/* on dev.bot.wwbp.org and bot.wwbp.org; see client_ip.
PUBLIC_PROXY_HOPS = 1

READ_METHODS = ("GET", "HEAD")
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def classify(method, path):
    """Map a request to an AccessLog action, or None if it is not logged."""
    method = method.upper()
    if method not in READ_METHODS + WRITE_METHODS or path in AUTH_PAGES:
        return None
    if path.startswith(ADMIN):
        # django-import-export 4.x ExportMixin: GET <model>/export/ renders the
        # format/field form; POST to the same URL returns the file.
        if path.endswith("/export/") and method == "POST":
            return Action.EXPORT
        return Action.VIEW if method in READ_METHODS else Action.CHANGE
    if not path.startswith(STAFF_API):
        return None
    # GET /api/avatar/<bot>/ is public: the participant chat page loads it.
    if (
        method in READ_METHODS
        and path.startswith("/api/avatar/")
        and path != "/api/avatar/"
    ):
        return None
    return Action.VIEW if method in READ_METHODS else Action.CHANGE


def client_ip(request):
    """
    The client address CloudFront saw.

    Requests go client -> CloudFront -> ALB -> nginx -> gunicorn, and each hop
    appends its peer to X-Forwarded-For, so the leftmost entries are whatever
    the client sent and cannot be trusted. Walking from the right: our own hops
    are private (VPC 10.x, Docker 172.x) and skipped, the first public address
    is the CloudFront edge the ALB saw, and the next is the client.
    """
    # ponytail: trusts one public hop (CloudFront). Safe only while the ALB
    # accepts CloudFront alone (its security group allows just the managed
    # prefix list com.amazonaws.global.cloudfront.origin-facing, since
    # 2026-11-02); if it is reopened to the internet, this can be forged.
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    public = []
    for entry in reversed([e.strip() for e in forwarded.split(",") if e.strip()]):
        try:
            ip = ipaddress.ip_address(entry)
        except ValueError:
            break
        if ip.is_global:
            public.append(str(ip))
        if len(public) > PUBLIC_PROXY_HOPS:
            return public[-1]
    if public:
        return public[-1]  # fewer hops than expected (no CDN): closest we have
    try:
        return str(ipaddress.ip_address(request.META.get("REMOTE_ADDR", "")))
    except ValueError:
        return None


def _row(request, user, action, username=None, status_code=None, path=None):
    # Signals may pass no request (authenticate() without one) or a bare
    # HttpRequest with method None (force_login).
    request = request or HttpRequest()
    if username is None:
        username = user.get_username()
    return {
        "user": user,
        "username": username[:150],
        "action": action,
        "method": request.method or "",
        "path": (path or request.path)[:1024],
        "query": request.META.get("QUERY_STRING", "")[:1024],
        "status_code": status_code,
        "ip": client_ip(request),
    }


def _write(request, user, action, **kwargs):
    # Swallowed on purpose: an audit-row failure must never turn a working admin
    # page (or a login) into a 500. It still reaches the error log.
    try:
        # Savepoint, so a failed insert cannot poison a caller's transaction.
        with transaction.atomic():
            AccessLog.objects.create(**_row(request, user, action, **kwargs))
    except Exception:
        logger.exception("access log write failed (%s)", action)


class AccessLogMiddleware:
    """
    Writes an AccessLog row after the response for admin and staff-API requests
    made by a logged-in user. Runs after AuthenticationMiddleware.
    """

    async_capable = True
    sync_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        if iscoroutinefunction(self.get_response):
            markcoroutinefunction(self)

    def __call__(self, request):
        # Django calls __call__ on both stacks; it never picks __acall__ itself.
        # Under ASGI this sits below WhiteNoise, so it does run async in prod.
        if iscoroutinefunction(self):
            return self.__acall__(request)
        response = self.get_response(request)
        action = classify(request.method, request.path)
        if action and request.user.is_authenticated:
            _write(request, request.user, action, status_code=response.status_code)
        return response

    async def __acall__(self, request):
        response = await self.get_response(request)
        action = classify(request.method, request.path)
        if action:
            try:
                # request.user is lazy and would hit the ORM synchronously here.
                user = await request.auser()
                if user.is_authenticated:
                    await AccessLog.objects.acreate(
                        **_row(request, user, action, status_code=response.status_code)
                    )
            except Exception:
                logger.exception("access log write failed (%s)", action)
        return response


@receiver(user_logged_in)
def _on_login(sender, request, user, **kwargs):
    _write(request, user, Action.LOGIN)


@receiver(user_logged_out)
def _on_logout(sender, request, user, **kwargs):
    # user is None when the session had already expired — nothing to record.
    if user is not None:
        _write(request, user, Action.LOGOUT)


@receiver(user_login_failed)
def _on_login_failed(sender, credentials, request=None, **kwargs):
    # credentials also holds the (already-masked) password: take the name only.
    username = str(credentials.get(get_user_model().USERNAME_FIELD, ""))
    _write(request, None, Action.LOGIN_FAILED, username=username)
