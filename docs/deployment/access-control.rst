Access Control
==============

Who can sign in to the admin panel (``/api/admin/``) and what they can do there.

Roles
-----

Each person gets their own login and only the access they need. Bot
configuration and participant data are separate roles; someone who needs both
is put in both groups. Roles apply to the whole site: a role grants the same
access to every bot and every study.

.. list-table::
   :header-rows: 1
   :widths: 16 28 28 28

   * - Role
     - Configuration (bots, personas, moderation settings)
     - Participant data (conversations, utterances, keystrokes, survey
       responses, avatars)
     - Security (accounts, survey ingest tokens)
   * - **Config editor**
     - View, add, change
     - —
     - —
   * - **Data viewer**
     - View
     - View and export
     - —
   * - **Superuser**
     - Everything, including delete
     - Everything, including delete
     - Everything

Avatars count as participant data because they are generated from
participants' faces. A Config editor still sets a bot's default avatar, from
the bot's own page. Nothing can be deleted except by a superuser. Keep the
number of superusers small; the ``admin`` account created at deployment is one.

The roles are created by a database migration, so they exist as soon as the
site is deployed. Do not edit their permissions in the admin: change them with
a new migration so every deployment stays the same.

Adding a person
~~~~~~~~~~~~~~~

Sign in as a superuser, then:

1. Go to **Authentication and Authorization → Users → Add user**.
2. Enter a username (e.g. their PennKey) and a temporary password, then click
   **Save**.
3. On the next page, under **Permissions**:

   - tick **Staff status** — without it they cannot sign in to the admin;
   - leave **Superuser status** unticked;
   - under **Groups**, move *Config editor*, *Data viewer* or both to
     *Chosen groups*.

4. Click **Save**, and send them the username and temporary password. They can
   change it from **Change password** in the top-right of the admin.

To change someone's role, edit their user and change their groups. To remove
access, untick **Active** rather than deleting the user, so their name stays
attached to their past actions.

Edit history
------------

Every change made to a study's configuration is recorded: who made it, when,
and which fields changed from what to what. If a bot's prompt was edited in the
middle of a study, this is how you find out who changed it and what it said
before.

**Where to see it:** open any bot, persona, survey ingest token or the global
moderation setting in the admin panel and click the **History** button in the
top right. Each row lists the user, the time and the changed fields; click a
row to see the full record as it was at that moment, and revert to it if you
need to.

**What is tracked**

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Admin section
     - Notes
   * - Bots
     - Every field, including which personas the bot uses.
   * - Personas
     - Every field.
   * - Global Moderation Settings
     - Whether moderation is on or off.
   * - Survey ingest tokens
     - The note and whether the token is active. The token itself is *not*
       copied into history: it never changes, and every extra copy is
       somewhere else it could leak from.

Changes made outside the admin panel are recorded too, with no user. In
particular, every server start re-applies the prompts of the bots listed in
``api/config.json``, so those bots get a user-less history entry at each
restart, and an admin edit to their prompt is overwritten there.

**What is not tracked, and why**

Conversations, utterances, keystrokes, survey responses and avatars are written
by participants as they chat, not by researchers. A history copy of each of
those writes would roughly double the largest tables in the database for no
audit benefit. Avatars are on this list because a participant's avatar is
created and updated during their conversation; changing a bot's avatar
in the admin still shows up in that bot's history as a save by you.

Edits made before edit history was switched on have no history. Only changes
from then on are recorded.

Access log
----------

Django's admin history only records when something is added, changed, or
deleted. It cannot tell you who *looked at* or *downloaded* participant data.
The access log fills that gap: every action a signed-in user takes in the admin
(``/api/admin/``) or the staff-only API leaves one row in the
``chatbot_accesslog`` table.

**What is recorded**

- Every admin page a signed-in user opens (``view``), and every form they
  submit (``change``) — including saves and deletes.
- Every export from the admin's **Export** button (``export``). Opening the
  export form is a ``view``; the ``export`` row is written when the file is
  actually downloaded.
- Requests to the staff-only API used by the bot editor: ``/api/bots/``,
  ``/api/avatar/`` and ``/api/avatar-upload/``.
- Every sign-in (``login``), sign-out (``logout``), and failed sign-in
  (``login_failed``). A failed sign-in records the username that was typed.

Each row has the user (and a copy of their username, so the row survives the
account being deleted), the time, the action, the HTTP method, the page path
and query string (so a search for a participant ID is visible), the response
status, and the client IP address.

**What is deliberately not recorded**

- Request or response bodies, passwords, and tokens. A failed sign-in stores
  the attempted username only.
- Participant traffic: chat messages, keystrokes, survey answers, voice, and
  the public avatar image the chat page loads. These are not staff activity and
  would bury the log. Researchers testing a bot while signed in are not logged
  either, for the same reason.
- Anything done outside the web app, such as a direct database connection or
  ``manage.py shell``.

**How to review it**

Sign in as a superuser and open **Access logs** in the admin. Filter by user,
action, or date, or search by username or path — for example, search
``conversation/export`` to see every conversation export. The page is visible
to superusers only, and nobody — superusers included — can edit or delete a
row from the admin.

The IP address is the one CloudFront saw, read from ``X-Forwarded-For`` from
the right so a value the client invents is ignored. A request sent straight to
the load balancer, bypassing CloudFront, could still forge it; treat the IP as
a strong hint, not proof.

**Retention**

Rows are never pruned automatically yet, so the table grows with admin use. If
it needs trimming, decide on a retention period with the study's IRB
requirements in mind before deleting anything.
