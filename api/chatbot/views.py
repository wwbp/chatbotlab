import json
import logging

from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from .services.post_processing import (
    _DEFAULT_BOT_CONFIG,
    calculate_typing_delays,
    human_like_chunks,
)
from .services.runchat import BotNotFound, ConversationNotFound, run_chat_round

logger = logging.getLogger(__name__)


def health_check(request):
    return JsonResponse({"status": "ok"})


@csrf_exempt
def test_upload(request):
    """Simple test endpoint to check if file uploads work"""
    if request.method == "POST":
        if request.FILES:
            file_info = []
            for field_name, uploaded_file in request.FILES.items():
                file_info.append(
                    {
                        "field_name": field_name,
                        "file_name": uploaded_file.name,
                        "file_size": uploaded_file.size,
                        "content_type": uploaded_file.content_type,
                    },
                )
            return JsonResponse(
                {
                    "status": "success",
                    "message": "File upload test successful",
                    "files": file_info,
                },
            )
        else:
            return JsonResponse(
                {
                    "status": "error",
                    "message": "No files received",
                },
                status=400,
            )
    else:
        return JsonResponse(
            {
                "status": "error",
                "message": "Only POST method allowed",
            },
            status=405,
        )


@method_decorator(csrf_exempt, name="dispatch")
class ChatbotAPIView(View):
    async def post(self, request, *args, **kwargs):
        # Hoisted above the try so they're available for error logging even if
        # body parsing itself fails.
        conversation_id = bot_name = participant_id = None
        try:
            data = json.loads(request.body)
            message = data.get("message", "").strip()
            bot_name = data.get("bot_name", "").strip()
            conversation_id = data.get("conversation_id")
            participant_id = data.get("participant_id")

            if not message or not bot_name or not conversation_id:
                return JsonResponse({"error": "Missing required fields."}, status=400)

            # run_chat_round returns (response_text, bot) — bot already fetched inside,
            # no second DB query needed here.
            response_text, bot = await run_chat_round(
                bot_name=bot_name,
                conversation_id=conversation_id,
                participant_id=participant_id,
                message=message,
            )

            if bot is not None:
                use_chunks = bot.chunk_messages
                use_humanlike_delay = bot.humanlike_delay
                response_chunks = (
                    human_like_chunks(response_text) if use_chunks else [response_text]
                )
                delay_data = calculate_typing_delays(message, response_chunks, bot)
            else:
                # bot is None only for the [FOLLOW-UP REQUEST] guard path (shouldn't
                # reach here in practice, but handle gracefully).
                use_chunks = True
                use_humanlike_delay = True
                response_chunks = [response_text]
                delay_data = calculate_typing_delays(
                    message, response_chunks, _DEFAULT_BOT_CONFIG
                )

            delay_config = {
                "reading_time": delay_data["reading_time"],
                "min_reading_delay": delay_data["min_reading_delay"],
                "response_segments": delay_data["response_segments"],
            }

            return JsonResponse(
                {
                    "message": message,
                    "response": response_text,
                    "response_chunks": response_chunks,  # Keep for backward compatibility
                    "bot_name": bot_name,
                    "humanlike_delay": use_humanlike_delay,
                    "chunk_messages": use_chunks,
                    "delay_config": delay_config,
                },
                status=200,
            )

        except BotNotFound:
            # A bot_name with no row is a client error, not a server fault.
            # 404 matches what /api/initialize_conversation/ and the avatar
            # endpoints already return for exactly this condition. Imported as
            # an exception rather than caught as Bot.DoesNotExist so this module
            # still does not import the Bot model — see
            # test_views_does_not_import_bot_model.
            logger.warning(
                "ChatbotAPIView: bot not found "
                "[conversation_id=%s bot_name=%s participant_id=%s]",
                conversation_id,
                bot_name,
                participant_id,
            )
            return JsonResponse(
                {"error": f"No bot found with the name '{bot_name}'."},
                status=404,
            )

        except ConversationNotFound:
            # Missing conversation row is a client error (never initialized /
            # deleted), not a server fault — return 400, not a generic 500.
            logger.warning(
                "ChatbotAPIView: conversation not found "
                "[conversation_id=%s bot_name=%s participant_id=%s]",
                conversation_id,
                bot_name,
                participant_id,
            )
            return JsonResponse(
                {
                    "error": "Conversation not found. "
                    "Please initialize the conversation first."
                },
                status=400,
            )

        except Exception as e:
            # logger.exception attaches the full traceback (exc_info) at ERROR
            # level; we also surface the exception TYPE and request context so a
            # 500 is diagnosable from one log line instead of a bare message.
            logger.exception(
                "❌ [ERROR] ChatbotAPIView %s: %s "
                "[conversation_id=%s bot_name=%s participant_id=%s]",
                type(e).__name__,
                e,
                conversation_id,
                bot_name,
                participant_id,
            )
            return JsonResponse({"error": "An unexpected error occurred."}, status=500)
