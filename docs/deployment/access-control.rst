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

.. note::
   You must be a superuser to do this procedure.

1. Go to **Authentication and Authorization → Users → Add user**.
2. Enter the email address of the person.
3. Enter their first name and last name.
4. Under **Role**, select *Config editor*, *Data viewer*, or both.
5. Click **Save**.

The person gets an invite email with a link. The link sets their password. The
link works one time and expires after 3 days. Their username is their email
address in lowercase.

.. note::
   If the email is not sent, the admin shows a warning. The user is saved.
   Do the procedure in *Sending a new link*.

Sending a new link
~~~~~~~~~~~~~~~~~~

Do this procedure if the person did not get the email, or the link expired.

.. note::
   The new email makes all earlier invite links unusable.

1. Go to **Authentication and Authorization → Users**.
2. Select the person.
3. In **Action**, select **Send set-password email**.
4. Click **Go**.

People who forget their password can get a link without help. On the admin
sign-in page, they click **Forgotten your login credentials?** and enter
their email address.

Changing a role
~~~~~~~~~~~~~~~

1. Open the user.
2. Change their **Groups**.
3. Click **Save**.

Removing access
~~~~~~~~~~~~~~~

.. warning::
   Do not delete the user. Their name must stay on their past actions.

1. Open the user.
2. Clear **Active**.
3. Click **Save**.

Their session stops at their next request.

Switching from the shared login
-------------------------------

Do this procedure one time, after these changes are on production.

1. Sign in with the shared ``admin`` login.
2. Add each person. Use the procedure in *Adding a person*.
3. Tell each person to set their password from the invite email.
4. In the access log, find the ``password_set`` row for each person.
5. For each person without a ``password_set`` row, send a new link.

.. note::
   Keep the new ``admin`` password with the lab's other secrets. Use it only
   in an emergency. Do not share it.

6. Change the ``admin`` password with **Change password** (top right).
7. For some days, look for ``login_failed`` rows on ``admin`` in the access
   log. These rows show who still uses the old password.

Email
-----

Set these environment variables on each Elastic Beanstalk environment:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Variable
     - Value
   * - ``SITE_URL``
     - The public URL, e.g. ``https://bot.wwbp.org``. Links use this URL.
       If it is not set, invites fail with a warning.
   * - ``EMAIL_BACKEND``
     - ``chatbot.email_backend.SESBackend``. The default prints emails to the
       console.
   * - ``DEFAULT_FROM_EMAIL``
     - The sender. Default: ``ChatbotLab <no-reply@bot.wwbp.org>``. Staging:
       ``ChatbotLab (staging) <no-reply@bot.wwbp.org>``.

.. note::
   Verify the sender address in Amazon SES before you send email.

The links are not in the access log. Each link is in the nginx, load balancer
and CloudFront access logs one time. The links work one time and expire after
3 days.

Sign-in protection
------------------

The admin sign-in page limits attempts in each 15-minute period:

- 10 attempts for one username.
- 30 attempts from one IP address.

The limits count all attempts, not only failed attempts. At the limit, the page
refuses more attempts from that username or IP address. Wait 15 minutes. Then
try again.

The "Forgotten your login credentials?" page sends a maximum of 3 emails to one
address, and 10 emails from one IP address, in each hour. At the limit, the page
shows the usual message but sends no email.

Passwords must have a minimum of 12 characters. A session stops after 12 hours.
Then you must sign in again.

The admin does not use multi-factor authentication (MFA) at this time. The lab
asked Penn IRB and ISO if these studies must have MFA.

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
- Every password reset request (``password_reset_requested``, with the email
  that was typed) and every password set from a link (``password_set``).

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
the right so a value the client invents is ignored. This relies on the load
balancer accepting traffic from CloudFront only: on dev.bot.wwbp.org and
bot.wwbp.org its security group allows just AWS's CloudFront prefix list
(``com.amazonaws.global.cloudfront.origin-facing``) on port 443. If that rule
is ever reopened to ``0.0.0.0/0``, the IP can be forged.

**Retention**

Rows are never pruned automatically, so the table grows with admin use. Agree a
retention period with the study's IRB first, then delete older rows by hand
(``--dry-run`` shows the count without deleting)::

    python manage.py prune_access_log --older-than-days 365 --dry-run
    python manage.py prune_access_log --older-than-days 365
