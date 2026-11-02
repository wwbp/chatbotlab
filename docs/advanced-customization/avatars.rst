Participant Avatars
===================

A bot whose **Avatar type** is ``user`` shows each participant an avatar
generated from their own photo. Use it for studies on how a bot's appearance
affects the conversation.

How it works
------------

1. Send the participant to the upload page first (e.g. from Qualtrics)::

       https://bot.wwbp.org/avatar-upload?bot_name=<bot>&conversation_id=<id>&participant_id=<id>

   They upload a PNG or JPEG photo. The bot's *Avatar prompt* turns it into an
   avatar (OpenAI ``gpt-image-1``), and **the photo is then deleted**. Only the
   generated avatar is kept.
2. Start the chat with the same ``bot_name`` and ``conversation_id``. Add
   ``condition`` to choose what the bot looks like:

   - none (default): the participant's own avatar;
   - ``dissimilar``: another participant's avatar, picked at random;
   - ``control``: the fixed image in ``CHATBOT_CONTROL_IMAGE``.

Each conversation gets one avatar. A second upload for the same
``conversation_id`` is refused, because every avatar is a paid image
generation. Participants do not log in. Only ``user`` avatar bots accept
uploads from them.

Server settings
---------------

``AWS_BUCKET_NAME`` (a private S3 bucket that allows ``PUT`` from the site's
origin), ``AWS_REGION``, ``OPENAI_API_KEY`` and ``CHATBOT_CONTROL_IMAGE`` (the
key of an image under ``avatar/`` in that bucket). Without the bucket, uploads
fail.
