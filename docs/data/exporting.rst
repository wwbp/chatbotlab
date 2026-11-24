Exporting Data
==============

ChatbotLab provides multiple ways to export conversation and participant data
for analysis.

Admin Exports
-------------

From the admin interface, select a model, study, or participant range and
choose:

- **CSV Export:** Tabular data suitable for R, Python, or Excel
- **JSON Export:** Structured data for downstream scripts
- **ZIP Archive:** Full session data including logs
- **Programmatic Export:** Use Django ORM or custom scripts to export data from
  the backend.

Direct Database Exports
-----------------------

Developers can also export data directly from the MySQL database. Use:

.. code-block:: bash

   mysqldump -u <user> -p chatlab_db > chatlab_data.sql

Example Script
--------------

.. code-block:: python

   from chatbot.models import Conversation, Utterance
   import csv

   with open("conversations.csv", "w") as f:
       writer = csv.writer(f)
       writer.writerow(["conversation_id", "participant_id", "start_time"])
       for convo in Conversation.objects.all():
           writer.writerow([convo.id, convo.participant_id, convo.start_time])

Formula escaping
----------------

Spreadsheet programs run a cell that starts with ``=``, ``+``, ``-``, ``@``, a
tab or a carriage return as a formula, and participants type the cells. In
**CSV and TSV** exports, a text cell that starts with one of these gets one
apostrophe (``'``) put in front, which spreadsheets read as "this is text".
Nothing is removed and the rest of the cell is unchanged. Numbers (such as
``-1``), dates and empty cells are never changed. JSON, YAML and HTML exports
are not escaped.

IDs, study names and conditions are text, so one that starts with such a
character is escaped too, the same way in every file, so files still join.
A cell that already began with an apostrophe followed by one of these
characters gets one more, so the original can always be recovered. To get the
text exactly as typed in pandas:

.. code-block:: python

   df = pd.read_csv("Utterance.csv", encoding="utf-8-sig")
   df["text"] = df["text"].str.replace(r"^'(?='*[=+\-@\t\r])", "", regex=True)

Privacy and Ethics
------------------

Always anonymize participant identifiers and remove sensitive metadata before
sharing data. Align exports with your IRB's data management plan.
