Access Control
==============

Who can sign in to the admin panel (``/api/admin/``) and what they can do there.

Roles
-----

Each person gets their own login and one of two roles. Roles apply to the whole
site: a role grants the same access to every bot and every study.

.. list-table::
   :header-rows: 1
   :widths: 20 40 40

   * - Role
     - Can
     - Cannot
   * - **Researcher**
     - View conversations, utterances, keystrokes, survey responses, bots and
       personas. Export conversations, utterances and bots (the models with an
       *Export* button).
     - Add, change or delete anything.
   * - **Bot editor**
     - Everything a Researcher can, plus add and change bots, personas, avatars
       and moderation settings — in the admin.
     - Delete anything, including bots and avatars.
   * - **Superuser**
     - Everything, including managing accounts and survey ingest tokens.
     - —

Only superusers manage accounts and survey ingest tokens. Keep the number of
superusers small; the ``admin`` account created at deployment is one.

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
   - under **Groups**, move *Researcher* or *Bot editor* to *Chosen groups*.

4. Click **Save**, and send them the username and temporary password. They can
   change it from **Change password** in the top-right of the admin.

To change someone's role, edit their user and swap the group. To remove access,
untick **Active** rather than deleting the user, so their name stays attached
to their past actions.
