import time

from asgiref.sync import async_to_sync
from django.core.management.base import BaseCommand, CommandError

from chatbot.models import Model
from chatbot.services.runchat import kani_reply
from server.engine import initialize_engine_from_model

SYSTEM_PROMPT = "You are a connectivity check."
QUERY = "Reply with the single word OK."


class Command(BaseCommand):
    help = (
        "Report which models answer: one tiny real call per active model, through "
        "the same engine path as a participant's message. Never changes the "
        "database. Exits non-zero if any active model fails."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--all",
            action="store_true",
            help=(
                "Also check models not offered for new bots (their failures "
                "don't fail the run)."
            ),
        )

    def handle(self, *args, **options):
        models = Model.objects.select_related("provider").order_by(
            "provider__name", "model_id"
        )
        if not options["all"]:
            models = models.filter(is_active=True, provider__is_active=True)

        failed_active = []
        for model in models:
            start = time.monotonic()
            try:
                # No max_tokens/temperature here on purpose: the check sends
                # exactly what the app sends, so a parameter a model rejects
                # shows up as a FAIL. The prompt keeps the reply short.
                engine = initialize_engine_from_model(model)
                reply = async_to_sync(kani_reply)(engine, SYSTEM_PROMPT, [], QUERY)
                if not reply:
                    raise RuntimeError("empty reply (no text returned)")
                status, detail = "OK", repr(reply[:40])
            except Exception as e:  # report every failure, keep checking the rest
                status = "FAIL"
                first_line = (str(e).strip().splitlines() or [""])[0]
                detail = f"{type(e).__name__}: {first_line}"[:200]
                if not model.retired:
                    failed_active.append(model.model_id)
            ms = round((time.monotonic() - start) * 1000)
            retired = " (not offered)" if model.retired else ""
            line = (
                f"{model.provider.name:<10} {model.model_id + retired:<52} "
                f"{status:<4} {ms:>6}ms  {detail}"
            )
            style = self.style.SUCCESS if status == "OK" else self.style.ERROR
            self.stdout.write(style(line))

        if failed_active:
            raise CommandError(
                f"{len(failed_active)} active model(s) failed: "
                + ", ".join(failed_active)
            )
