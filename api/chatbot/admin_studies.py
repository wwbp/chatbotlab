"""
The Studies page: one row per study, and one zip export per study.

There is no Study model. A study is every Conversation that shares a
study_name, a free-text value the survey tool sends when a participant starts.
This page groups on it so a researcher does not have to find the study in four
sidebar filters and export four tables one at a time.

Wired into the admin by ChatbotLabAdminSite.get_urls (admin_site.py).
"""

import datetime as dt
import io
import zipfile

from django import forms
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Exists, Max, Min, OuterRef
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.http import content_disposition_header, urlencode
from django.utils.text import slugify
from django.views.decorators.http import require_POST
from import_export.formats import base_formats

from .models import Conversation, Keystroke, SurveyResponse, Utterance


def in_date_range(queryset, field, start, end):
    """Rows whose `field` falls on a day from start to end, both included.

    Compared as datetimes rather than with __date, so an index on the column
    can be used.
    """

    def midnight(day):
        return timezone.make_aware(dt.datetime.combine(day, dt.time.min))

    if start:
        queryset = queryset.filter(**{f"{field}__gte": midnight(start)})
    if end:
        queryset = queryset.filter(
            **{f"{field}__lt": midnight(end + dt.timedelta(days=1))}
        )
    return queryset


class StudyFilterForm(forms.Form):
    """The page's filters. Field names double as the query-string parameters of
    the page, the export, and the Conversation list the rows link to."""

    # Not a ChoiceField: the export validates the same form, and should not
    # need a query for the list of conditions to do it.
    user_group = forms.CharField(
        label="Condition", required=False, strip=False, widget=forms.Select
    )
    started_time_from = forms.DateField(
        label="Started from",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    started_time_to = forms.DateField(
        label="Started to",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )

    def conversations(self):
        data = self.cleaned_data
        queryset = in_date_range(
            Conversation.objects.all(),
            "started_time",
            data["started_time_from"],
            data["started_time_to"],
        )
        if data["user_group"]:
            queryset = queryset.filter(user_group=data["user_group"])
        return queryset

    def params(self):
        """The filters that are set, as query-string parameters."""
        return {
            name: value if isinstance(value, str) else value.isoformat()
            for name, value in self.cleaned_data.items()
            if value
        }


def _url(name, params):
    return reverse(f"admin:{name}") + "?" + urlencode(params)


def studies_view(request):
    if not request.user.has_perm("chatbot.view_conversation"):
        raise PermissionDenied

    form = StudyFilterForm(request.GET)
    conditions = (
        Conversation.objects.exclude(user_group__isnull=True)
        .exclude(user_group="")
        .values_list("user_group", flat=True)
        .distinct()
        .order_by("user_group")
    )
    form.fields["user_group"].widget.choices = [("", "All conditions")] + [
        (c, c) for c in conditions
    ]

    studies = {}
    if form.is_valid():
        # One grouped query however many studies there are. Counting
        # utterances joins the largest table, hence distinct on conversations.
        rows = (
            form.conversations()
            .values("study_name", "user_group")
            .annotate(
                conversations=Count("pk", distinct=True),
                utterances=Count("utterances"),
                first=Min("started_time"),
                last=Max("started_time"),
            )
            .order_by("user_group")
        )
        filters = form.params()
        # ponytail: rows are merged per study on the exact string, while the
        # database groups (and the links below filter) with its collation,
        # which ignores case. "Stress" and "stress" with no condition in common
        # would show as two rows that open the same list. Group by study alone
        # in a second query if that ever happens in practice.
        for row in rows:
            name = row["study_name"]
            if name not in studies:
                # Same lookups Django's own sidebar filter uses, so the links
                # behave exactly like picking the study there.
                study = (
                    {"study_name__isnull": "True"}
                    if name is None
                    else {"study_name": name}
                )
                by_conversation = {
                    f"conversation__{k}": v
                    for k, v in {**study, **filters}.items()
                    if not k.startswith("started_time")
                }
                studies[name] = {
                    "name": name,
                    "label": name or ("(no study name)" if name is None else "(empty)"),
                    "conversations": 0,
                    "utterances": 0,
                    "conditions": [],
                    "first": row["first"],
                    "last": row["last"],
                    "conversations_url": _url(
                        "chatbot_conversation_changelist", {**study, **filters}
                    ),
                    # Dates are left off: they select conversations by start
                    # date, and the Utterance list can only filter on its own.
                    "utterances_url": _url(
                        "chatbot_utterance_changelist", by_conversation
                    ),
                    "export_url": _url("studies_export", {**study, **filters}),
                }
            study = studies[name]
            study["conversations"] += row["conversations"]
            study["utterances"] += row["utterances"]
            study["conditions"].append((row["user_group"], row["conversations"]))
            study["first"] = min(study["first"], row["first"])
            study["last"] = max(study["last"], row["last"])

    # ponytail: every study on one page. Fine for hundreds; paginate if a
    # deployment ever collects thousands of distinct study names.
    return TemplateResponse(
        request,
        "admin/studies.html",
        {
            **admin.site.each_context(request),
            "title": "Studies",
            "form": form,
            "studies": sorted(studies.values(), key=lambda s: s["last"], reverse=True),
            "filtered": bool(form.is_valid() and form.params()),
        },
    )


EXPORT_FILES = (
    ("conversations.csv", Conversation),
    ("utterances.csv", Utterance),
    ("keystrokes.csv", Keystroke),
    ("survey_responses.csv", SurveyResponse),
)


# POST, like the per-table exports: a download of participant data should not
# be something a link, a prefetch or a reload can trigger, and it puts the
# request under CSRF protection. The filters travel in the query string so the
# access log (which never records bodies) shows which study was exported.
@require_POST
def studies_export_view(request):
    # Each table's own admin decides, so this can never export a table the
    # user could not export from its list page.
    allowed = [
        (filename, model_admin)
        for filename, model in EXPORT_FILES
        if (model_admin := admin.site.get_model_admin(model)).has_export_permission(
            request
        )
    ]
    if not allowed:
        raise PermissionDenied

    form = StudyFilterForm(request.GET)
    if "study_name" in request.GET:
        study = {"study_name": request.GET["study_name"]}
    elif request.GET.get("study_name__isnull") == "True":
        study = {"study_name__isnull": True}
    else:
        return HttpResponseBadRequest("study_name is required.")
    if not form.is_valid():
        return HttpResponseBadRequest("Dates must be YYYY-MM-DD.")

    conversations = form.conversations().filter(**study)
    if not conversations.exists():
        raise Http404("No conversations match this study and these filters.")

    querysets = {
        Conversation: conversations,
        Utterance: Utterance.objects.filter(conversation__in=conversations),
        # Neither table has a ForeignKey (rows can arrive before the
        # conversation exists), so they are matched the way the app itself
        # matches them: keystrokes by conversation_id, survey responses by the
        # (survey_id, participant_id) pair initialize_conversation looks up.
        # Rows that never met a conversation belong to no study and are only
        # in their own table's export.
        Keystroke: Keystroke.objects.filter(
            conversation_id__in=conversations.values("conversation_id")
        ),
        SurveyResponse: SurveyResponse.objects.filter(
            Exists(
                conversations.filter(
                    survey_id=OuterRef("survey_id"),
                    participant_id=OuterRef("participant_id"),
                )
            )
        ),
    }

    # ponytail: the whole zip (and each CSV) is built in memory. Fine up to
    # tens of MB (roughly 100k utterances); beyond that a worker can run out
    # of memory or the request can time out. Upgrade path: write the CSVs
    # row by row into a streamed zip (StreamingHttpResponse), or build the
    # file in a background job and hand out a link.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, model_admin in allowed:
            # The admin's own export: same Resource, columns and Excel BOM as
            # the Export button on that table's list.
            data = model_admin.get_export_data(
                base_formats.CSV(),
                request,
                querysets[model_admin.model].order_by("pk"),
            )
            archive.writestr(filename, data)

    name = slugify(request.GET.get("study_name", "")) or "unnamed"
    return HttpResponse(
        buffer.getvalue(),
        content_type="application/zip",
        headers={
            "Content-Disposition": content_disposition_header(True, f"study-{name}.zip")
        },
    )
