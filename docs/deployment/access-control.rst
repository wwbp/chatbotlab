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
