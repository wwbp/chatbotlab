Exporting Data
==============

Exports are made from the admin panel (``https://<your-domain>/api/admin``).
You need the **Data viewer** role, or a superuser account. Every export is
recorded in the access log with who made it and when.

One study at a time
-------------------

**Participant data → Studies** lists every study (each distinct ``study_name``
your survey tool sent) with its number of conversations, the split by
condition, the number of utterances and the first and last conversation dates.

1. Optionally narrow the page to one **condition** and a **started from / to**
   date range (both days included, in UTC). The counts update to match.
2. Press **Download zip** on the study's row.

The zip holds one CSV file per table, limited to the conversations counted in
that row:

- ``conversations.csv``: one row per conversation
- ``utterances.csv``: every message in those conversations
- ``keystrokes.csv``: keystroke logs with the same ``conversation_id``
- ``survey_responses.csv``: survey answers whose ``survey_id`` and
  ``participant_id`` both match one of those conversations

A file for a table you are not allowed to view is left out. A table with no
rows for the study is still included, with its header row only.

Keystrokes and survey responses are not linked to a conversation in the
database; they are matched on the IDs above. A keystroke log or survey
response that never matched a conversation (for example a participant who
answered the survey but never opened the chat) belongs to no study and is only
in that table's own export. If two studies reuse the same ``survey_id`` and
``participant_id``, the same survey response appears in both downloads.

Conversations that arrived without a study name are listed as ``n/a``.

One table at a time
-------------------

The **Conversations**, **Utterances**, **Keystrokes** and **Survey responses**
lists each have an **Export** button. It exports the rows currently listed, so
apply the search and sidebar filters first (study, condition, date range).
Choose the file format (CSV by default) and the columns on the next screen.

The columns are the same as in the study zip. Prompts, chat history and the
bot configuration snapshot are not exported, because they are several
kilobytes per row; they are on each row's page in the admin and in the
database.

Reading the files
-----------------

CSV files are UTF-8 with a byte-order mark so that Excel shows accents and
emoji correctly. R reads them as they are; in pandas pass
``encoding="utf-8-sig"``. ``condition`` is the ``user_group`` your survey tool
sent. The ``answers`` column of ``survey_responses.csv`` is a JSON list.

.. code-block:: python

   import json
   import zipfile

   import pandas as pd

   with zipfile.ZipFile("study-stress.zip") as z:
       conversations = pd.read_csv(z.open("conversations.csv"), encoding="utf-8-sig")
       utterances = pd.read_csv(z.open("utterances.csv"), encoding="utf-8-sig")
       surveys = pd.read_csv(z.open("survey_responses.csv"), encoding="utf-8-sig")

   surveys["answers"] = surveys["answers"].map(json.loads)
   messages = utterances.merge(conversations, on="conversation_id", suffixes=("", "_c"))

Cells are exported exactly as participants typed them. A spreadsheet program
that opens one of these CSV files can run a cell starting with ``=``, ``+``,
``-`` or ``@`` as a formula, and participants type the cells. Until the
exports escape such cells (planned as a separate change), analyse in R or
pandas and do not open participant text in Excel.

A very large study is built in memory before it downloads. If a download
times out, narrow it with the date range and download it in parts, or use the
database directly.

Direct database exports
-----------------------

Developers can read the database directly, for example for fields the admin
does not export. See :doc:`sql-guis` and :doc:`schema`.

.. code-block:: bash

   mysqldump -u <user> -p <database> > chatbotlab_data.sql

Privacy and ethics
------------------

Always anonymize participant identifiers and remove sensitive metadata before
sharing data. Align exports with your IRB's data management plan.
