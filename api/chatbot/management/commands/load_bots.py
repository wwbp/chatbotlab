import json

from django.core.management.base import BaseCommand

from chatbot.models import Bot, Model


class Command(BaseCommand):
    help = "Load bots from config.json into the database"

    def handle(self, *args, **kwargs):
        try:
            # Ensure default models exist first
            self.stdout.write("Ensuring default models exist...")
            Model.get_or_create_default_models()
            self.stdout.write(self.style.SUCCESS("Default models loaded."))

            with open("config.json") as file:
                config_data = json.load(file)

            bots = config_data.get("bots", [])
            if not bots:
                self.stdout.write(self.style.WARNING("No bots found in config.json."))
                return

            # Runs on every container start (entrypoint.sh), so only seed bots
            # that are missing: never overwrite what a researcher edited.
            for bot in bots:
                Bot.objects.get_or_create(
                    name=bot["name"],
                    defaults={
                        "prompt": bot["prompt"],
                        "ai_model": Model.objects.filter(
                            provider__name=bot.get("model_type"),
                            model_id=bot.get("model_id"),
                        ).first()
                        or Bot.get_default_model(),
                    },
                )
            self.stdout.write(
                self.style.SUCCESS("Bots successfully loaded into the database."),
            )
        except Exception as e:
            self.stderr.write(f"Error loading bots: {e}")
