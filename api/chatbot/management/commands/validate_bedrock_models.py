import logging
import os

import boto3
from botocore.exceptions import ClientError
from django.core.management.base import BaseCommand

from chatbot.models import Model

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Validate configured Amazon Bedrock models. Report-only unless --apply, "
        "which deactivates failing models. It never deletes a model."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--region",
            dest="region",
            default=os.environ.get("AWS_REGION", "us-east-1"),
            help="AWS region for the Bedrock runtime client.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Change the database. Without it the command only reports.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            dest="dry_run",
            help="Report only (the default; kept for old invocations).",
        )

    def handle(self, *args, **options):
        region = options["region"]
        # Report-only unless explicitly asked: a failed probe can be a transient
        # outage or missing credentials, not a dead model.
        dry_run = options["dry_run"] or not options["apply"]

        bedrock_models = Model.objects.filter(provider__name="Bedrock").order_by(
            "model_id"
        )
        total_models = bedrock_models.count()

        if total_models == 0:
            self.stdout.write(
                self.style.WARNING("No Bedrock models found to validate.")
            )
            return

        self.stdout.write(
            f"Validating {total_models} Bedrock models in region '{region}'..."
        )

        client = boto3.client("bedrock-runtime", region_name=region)
        active_models: list[tuple[int, str]] = []
        invalid_models: list[tuple[int, str, str]] = []

        conversation = [
            {
                "role": "user",
                "content": [{"text": "Bedrock connectivity check."}],
            },
        ]
        # No temperature/topP: newer Claude models reject them, which would
        # report a working model as invalid.
        inference_config = {"maxTokens": 8}

        for model in bedrock_models:
            try:
                client.converse(
                    modelId=model.model_id,
                    messages=conversation,
                    inferenceConfig=inference_config,
                )
                active_models.append((model.id, model.model_id))
                self.stdout.write(self.style.SUCCESS(f"[OK] {model.model_id}"))
            except ClientError as error:
                error_code = error.response.get("Error", {}).get("Code", "Unknown")
                error_message = error.response.get("Error", {}).get(
                    "Message", str(error)
                )
                invalid_models.append((model.id, model.model_id, error_message))
                self.stdout.write(
                    self.style.ERROR(
                        f"[INVALID] {model.model_id} -> {error_code}: {error_message}"
                    ),
                )

        if dry_run:
            self.stdout.write(
                self.style.WARNING("Report only, no changes applied (use --apply).")
            )
            return

        if not invalid_models:
            self.stdout.write(
                self.style.SUCCESS("All Bedrock models validated successfully.")
            )
            return

        for model_id, model_identifier, reason in invalid_models:
            model = Model.objects.filter(id=model_id).first()
            if not model:
                continue

            # Never delete: a probe fails for every model during an outage,
            # and rows are kept so old studies' records stay intact.
            model.is_active = False
            model.save(update_fields=["is_active"])
            action = "deactivated"

            logger.info(
                "Bedrock model %s (%s) %s after validation failure: %s",
                model_identifier,
                model.provider.name if model.provider else "Unknown provider",
                action,
                reason,
            )

            self.stdout.write(
                self.style.WARNING(
                    f"{model_identifier} {action} due to validation failure: {reason}",
                ),
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Validation finished. {len(active_models)} valid, {len(invalid_models)} invalid.",
            ),
        )
