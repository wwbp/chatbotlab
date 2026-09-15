import json
import logging
import random
from datetime import datetime

from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from ..models import Bot, Conversation, SurveyResponse, Utterance
from .runchat import save_chat_to_db

logger = logging.getLogger(__name__)


def _bot_public_config(bot):
    """
    Return the bot fields the frontend needs after conversation init.

    Only safe, non-sensitive fields — system prompt is intentionally excluded.
    This is returned by initialize_conversation so the frontend does not need
    to call the staff-only GET /api/bots/ endpoint.
    """
    return {
        "follow_up_on_idle": bot.follow_up_on_idle,
        "idle_time_minutes": bot.idle_time_minutes,
        "recurring_followup": bot.recurring_followup,
        "chunk_messages": bot.chunk_messages,
        "humanlike_delay": bot.humanlike_delay,
        "avatar_type": bot.avatar_type,
        "reading_words_per_minute": bot.reading_words_per_minute,
        "reading_jitter_min": bot.reading_jitter_min,
        "reading_jitter_max": bot.reading_jitter_max,
        "reading_thinking_min": bot.reading_thinking_min,
        "reading_thinking_max": bot.reading_thinking_max,
        "writing_words_per_minute": bot.writing_words_per_minute,
        "writing_jitter_min": bot.writing_jitter_min,
        "writing_jitter_max": bot.writing_jitter_max,
        "writing_thinking_min": bot.writing_thinking_min,
        "writing_thinking_max": bot.writing_thinking_max,
        "intra_message_delay_min": bot.intra_message_delay_min,
        "intra_message_delay_max": bot.intra_message_delay_max,
        "min_reading_delay": bot.min_reading_delay,
    }


async def randomly_select_persona(bot):
    """
    Randomly select one persona from the bot's assigned personas.
    Returns None if no personas are assigned.
    """
    assigned_personas = await sync_to_async(list)(bot.personas.all())
    if assigned_personas:
        selected_persona = random.choice(assigned_personas)
        logger.info(
            f"Randomly selected persona '{selected_persona.name}' for bot '{bot.name}'",
        )
        return selected_persona
    else:
        logger.info(f"No personas assigned to bot '{bot.name}', using default behavior")
        return None


async def load_conversation_history(conversation_id):
    """
    Load conversation history from database and populate cache.
    Returns the conversation history as a list of messages.
    """
    try:
        conversation = await Conversation.objects.aget(conversation_id=conversation_id)
        conversation_history = []
        messages = []

        async for utterance in Utterance.objects.filter(
            conversation=conversation
        ).order_by("created_time"):
            role = "user" if utterance.speaker_id == "user" else "assistant"
            content = utterance.text
            conversation_history.append({"role": role, "content": content})
            messages.append(
                {
                    "sender": "You" if role == "user" else "AI Chatbot",
                    "content": content,
                }
            )

        cache_key = f"conversation_cache_{conversation_id}"
        await cache.aset(cache_key, conversation_history, timeout=3600)

        logger.debug(
            f"Loaded {len(messages)} messages for conversation {conversation_id}",
        )
        return conversation, messages

    except Conversation.DoesNotExist:
        logger.debug(f"Conversation {conversation_id} not found")
        return None, []
    except Exception as e:
        logger.exception(f"Error loading conversation history: {e}")
        return None, []


async def _load_survey_context(survey_id, participant_id):
    """
    Fetch the answers the participant gave in the survey tool before arriving.

    Returns the list of {"question": ..., "answer": ...} entries, or None when
    no answers were delivered for this participant. None is a normal outcome,
    not an error: the bot simply gets no survey context appended, and the null
    column marks the conversation for whoever analyses the study.
    """
    if not survey_id or not participant_id:
        return None

    row = await SurveyResponse.objects.filter(
        survey_id=survey_id,
        participant_id=participant_id,
    ).afirst()

    if row is None:
        logger.info(
            "No survey response found for participant %s in survey %s",
            participant_id,
            survey_id,
        )
        return None

    return row.answers


@method_decorator(csrf_exempt, name="dispatch")
class InitializeConversationAPIView(View):
    async def post(self, request, *args, **kwargs):
        try:
            logger.debug("Entering InitializeConversationAPIView.post()")

            try:
                data = json.loads(request.body)
            except Exception as parse_error:
                logger.debug("JSON parse error: %s", parse_error)
                return JsonResponse(
                    {"error": "Invalid JSON in request body."},
                    status=400,
                )

            logger.debug("Received JSON data: %r", data)

            conversation_id = data.get("conversation_id")
            bot_name = data.get("bot_name")
            participant_id = data.get("participant_id")
            study_name = data.get("study_name", "n/a")
            user_group = data.get("user_group", "n/a")
            survey_id = data.get("survey_id", "n/a")
            survey_meta_data = json.dumps(data)

            if not bot_name or not conversation_id:
                return JsonResponse(
                    {"error": "Both 'bot_name' and 'conversation_id' are required."},
                    status=400,
                )

            try:
                bot = await Bot.objects.aget(name=bot_name)
                logger.debug("Found bot: %s", bot_name)
            except Bot.DoesNotExist:
                return JsonResponse(
                    {"error": f"No bot found with the name '{bot_name}'."},
                    status=404,
                )
            except Exception:
                logger.exception("Error fetching bot")
                return JsonResponse(
                    {"error": "Error fetching the bot from the database."},
                    status=500,
                )

            # Check if conversation already exists
            existing_conversation, existing_messages = await load_conversation_history(
                conversation_id,
            )

            if existing_conversation:
                logger.debug(
                    f"Conversation {conversation_id} already exists, returning existing data",
                )
                return JsonResponse(
                    {
                        "conversation_id": conversation_id,
                        "message": "Conversation loaded successfully.",
                        "initial_utterance": existing_conversation.initial_utterance
                        or "",
                        "existing_messages": existing_messages,
                        "is_existing": True,
                        "bot_config": _bot_public_config(bot),
                    },
                    status=200,
                )

            # Snapshot any survey answers collected before this conversation.
            # A copy, not a link: if the survey tool later overwrites the row,
            # this conversation must still show what the bot actually saw.
            survey_context = await _load_survey_context(survey_id, participant_id)

            # Randomly select a persona for this conversation
            selected_persona = await randomly_select_persona(bot)

            from django.core.serializers.json import DjangoJSONEncoder
            from django.forms.models import model_to_dict

            bot_config_data = await sync_to_async(model_to_dict)(
                bot, exclude=["personas"]
            )
            bot_config_data["personas"] = await sync_to_async(list)(
                bot.personas.values("id", "name")
            )
            bot_config = json.dumps(bot_config_data, cls=DjangoJSONEncoder)

            # Create new conversation
            try:
                await Conversation.objects.acreate(
                    conversation_id=conversation_id,
                    bot_name=bot.name,
                    bot_config=bot_config,
                    participant_id=participant_id,
                    initial_utterance=bot.initial_utterance,
                    study_name=study_name,
                    user_group=user_group,
                    survey_id=survey_id,
                    survey_meta_data=survey_meta_data,
                    survey_context=survey_context,
                    started_time=datetime.now(),
                    selected_persona=selected_persona,
                )
                logger.debug(
                    "Conversation created with persona: %s",
                    selected_persona.name if selected_persona else "None",
                )
            except Exception:
                logger.exception("Error creating Conversation")
                return JsonResponse(
                    {"error": "Failed to create Conversation."},
                    status=500,
                )

            # Save bot's initial utterance as an assistant message
            initial_messages = []
            if bot.initial_utterance and bot.initial_utterance.strip():
                try:
                    await save_chat_to_db(
                        conversation_id=conversation_id,
                        speaker_id="assistant",
                        text=bot.initial_utterance.strip(),
                        bot_name=bot.name,
                        participant_id=None,
                    )
                    logger.debug("Initial bot message saved to DB.")
                    initial_messages.append(
                        {
                            "sender": "AI Chatbot",
                            "content": bot.initial_utterance.strip(),
                        },
                    )
                except Exception as e:
                    logger.exception("Failed to save initial bot message: %s", str(e))

            return JsonResponse(
                {
                    "conversation_id": conversation_id,
                    "message": "Conversation initialized successfully.",
                    "initial_utterance": bot.initial_utterance or "",
                    "existing_messages": initial_messages,
                    "is_existing": False,
                    "bot_config": _bot_public_config(bot),
                },
                status=200,
            )

        except Exception:
            logger.exception("Unhandled exception in InitializeConversationAPIView")
            return JsonResponse({"error": "Unexpected error occurred."}, status=500)
