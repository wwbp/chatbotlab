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
