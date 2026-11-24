Viewing Data
============

You can review all collected conversation and participant data from the
**ChatbotLab admin panel** or by connecting to the database directly.

Admin panel
-----------

Access the admin interface at:

``https://<your-domain>/api/admin``

Participant data needs the **Data viewer** role (or a superuser account).
Under **Participant data** you can browse:

- **Studies:** one row per study name, with conversation, condition and
  utterance counts, links to that study's conversations and utterances, and a
  one-file download (see :doc:`exporting`)
- **Conversations:** one row per session; opening one shows the full
  transcript
- **Utterances:** individual messages with timestamps and moderation results
- **Survey responses:** answers the survey tool sent before the chat
- **Keystrokes:** typing and time-on-page logs

Bots and personas are under **Study setup**.

Filtering
---------

Start from **Studies** to jump to one study's conversations or utterances.
On the lists themselves, the sidebar filters are:

- **Conversations:** study, condition, bot, start date (presets, or a
  **date range** with a from and a to date, both included), and session health
  (no bot reply, two or fewer messages, no survey answers)
- **Utterances:** flagged by moderation, date range, study, condition, speaker
  and moderation category

The search box on Conversations takes an exact conversation ID or the start of
a participant ID. Dates are in UTC.

Export options
--------------

Download one study as a zip of CSV files from the Studies page, or use the
**Export** button on a list to export the rows it currently shows. See
:doc:`exporting`.
