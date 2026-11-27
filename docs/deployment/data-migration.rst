Data Migration Runbook
======================

How to move the live database from the hand-built Elastic Beanstalk environment (with its EB-managed RDS instance) to the Terraform-managed stack without losing or altering study data. **Rehearse it first, end to end, on the test stack**: Deploy Infrastructure with environment ``test`` builds it on demand in the same AWS account as production, the source is a restored copy of the production database snapshot in that account (see `Rehearsal source`_), and Destroy Infrastructure removes it afterwards. Only then run it for production.

.. warning::

   During a rehearsal the test stack, and the restored copy it is fed from, hold real participant data, in the account that also holds production. Keep both private and encrypted like production, use the test stack's own API keys (never production's), and destroy the stack and delete the restored copy and the copied snapshot when the rehearsal is done.

The copy is done and proven by ``scripts/migration/dbcopy.py``. What it guarantees, each backed by a test against real database servers (``api/chatbot/tests/data_migration/``):

- It cannot write to the source. Every session it opens there is ``READ ONLY`` and the server enforces it. **Rollback is therefore always "send traffic back to the old environment".**
- ``verify`` compares schema and every row (SHA-256 per cell, in primary-key order) and exits non-zero on any difference, naming the table and the first differing key. It refuses (rather than passing) when it cannot see everything: missing privileges, unreadable routine bodies, object types it cannot compare, fewer rows hashed than ``COUNT(*)``, a source with no tables, or source and target being the same database. See `What verify does not compare`_.
- Every step has ``--dry-run`` and appends to ``<run-dir>/run.log``. Keep the run directory: it is the record of the migration. Every file the tool writes there is mode 600. A differing row is named by its key only when the key is an integer; any other key (``django_session.session_key`` is a login token) is shown as ``sha256:`` and the first 12 hex digits of its hash.

.. warning::

   **Do not terminate the old Elastic Beanstalk environment.** Its database is managed by the environment: terminating the environment can delete the database with it. Scale it down or block it; decommission only at step 14.


Facts to establish first
------------------------

Do these days before the window. Each one can stop the plan.

1. **The engines must match.** Terraform creates MariaDB 10.11 (``infra/terraform/rds.tf``). The engine of the existing database is not recorded in this repository. Check both environments::

      aws rds describe-db-instances \
        --query 'DBInstances[].[DBInstanceIdentifier,Engine,EngineVersion,AllocatedStorage]' --output table

   The tool refuses to copy between MySQL and MariaDB (they store JSON differently and share no collations, so the copy could not be proven unchanged). If the old database is MySQL, change the Terraform engine to the same MySQL major version before going further. A MariaDB target older than the source is refused unless ``--allow-version-downgrade`` is passed to ``preflight``.

2. **Where the tool runs.** It needs to reach both databases on port 3306 at the same time, from a machine with Python 3.11+ and the MariaDB client (``mariadb``, ``mariadb-dump``); the backend Docker image has both. Both databases are in private subnets, so this is usually two port-forwards (SSM or SSH) through an instance of each environment. This path is **not** proven by the tests; prove it in the test-stack rehearsal with ``preflight --dry-run``.

3. **Uploaded files are not in the database.** Voice audio, avatars and uploads live in the S3 bucket named by ``AWS_BUCKET_NAME`` (production: ``bot-avatar.wwbp.org``, staging: ``dev.bot-avatar.wwbp.org``); the new stack has its own bucket (``<prefix>-data``). The database stores **keys relative to the bucket, never URLs**: ``chatbot_avatar.participant_avatar`` and ``chatbot_avatar.chatbot_avatar`` hold a key under ``avatar/`` (with or without the ``avatar/`` prefix; presigned URLs are made when a page is shown), and ``chatbot_utterance.audio_file`` holds ``utterance_audio/<name>`` (Django's S3 storage). So the cutover copies the objects **under the same keys** (step 9) and rewrites nothing. Confirm no row holds an absolute URL or the old bucket name; every count must be 0::

      SELECT 'participant_avatar', COUNT(*) FROM chatbot_avatar
        WHERE participant_avatar LIKE '%://%' OR participant_avatar LIKE '%bot-avatar%'
      UNION ALL SELECT 'chatbot_avatar', COUNT(*) FROM chatbot_avatar
        WHERE chatbot_avatar LIKE '%://%' OR chatbot_avatar LIKE '%bot-avatar%'
      UNION ALL SELECT 'audio_file', COUNT(*) FROM chatbot_utterance
        WHERE audio_file LIKE '%://%' OR audio_file LIKE '%bot-avatar%';

   After step 8, ``grep -c 'bot-avatar\.wwbp\.org' <run-dir>/dump.sql`` checks every other column too (a prompt or setting that names the bucket); it must print 0, or each hit must be rewritten by hand. Save the keys the database refers to as ``db.keys``, one per line, for step 9 (connection options as for the source database)::

      mariadb -N -B -e "
        SELECT audio_file FROM chatbot_utterance WHERE audio_file <> ''
        UNION SELECT IF(k LIKE 'avatar/%', k, CONCAT('avatar/', k)) FROM (
          SELECT participant_avatar AS k FROM chatbot_avatar
          UNION SELECT chatbot_avatar FROM chatbot_avatar) a
        WHERE k <> ''" <database> > db.keys

   ``CHATBOT_CONTROL_IMAGE`` (an environment variable, not a row) is also a key (``avatar/chatbot_control.png``) and is copied with the rest.

   Production has no ``AWS_BUCKET_NAME`` set today, so uploads there may have been failing: expect few or no avatar and audio objects in ``bot-avatar.wwbp.org`` (one, ``avatar/chatbot_control.png``, in November 2026) and rows whose files never reached S3. Those rows point at nothing before and after the cutover.

   **The new bucket only accepts browser uploads from the site's own origin** (CORS, ``infra/terraform/s3.tf``): ``https://<domain_name>``, or the CloudFront URL when ``domain_name`` is empty. Set ``domain_name`` to the public name (``bot.wwbp.org`` for production) before step 11, or participants' photo uploads fail after the switch.

4. **The app has no maintenance or read-only mode.** Taking it offline means stopping requests from reaching it (step 4).

5. **Same code on both sides.** Deploy the same git tag to the old and new environments before the window, so starting the new app applies no new migrations to the copy.


Setting up the tool
-------------------

Credentials come from the environment or from a client option file, never from the command line, and are never logged.

.. code-block:: bash

   export MIG_SOURCE_HOST=...  MIG_SOURCE_PORT=3306  MIG_SOURCE_DATABASE=...
   export MIG_SOURCE_USER=...  MIG_SOURCE_PASSWORD=...
   export MIG_TARGET_HOST=...  MIG_TARGET_PORT=3306  MIG_TARGET_DATABASE=chatbot_db
   export MIG_TARGET_USER=chatbot_user  MIG_TARGET_PASSWORD=...
   RUN="--run-dir migration-runs/rehearsal-$(date -u +%Y%m%d)"
   python3 scripts/migration/dbcopy.py $RUN preflight --dry-run

Instead of ``HOST``/``PORT``/``USER``/``PASSWORD`` you may set ``MIG_SOURCE_DEFAULTS_FILE`` (and ``MIG_TARGET_DEFAULTS_FILE``) to a ``chmod 600`` client option file with a ``[client]`` section.

**No other option file is read.** Every client and dump the tool starts gets ``--no-defaults``, or ``--defaults-file=<your file>`` when you set one: ``~/.my.cnf``, ``/etc/my.cnf`` and the like are ignored, because a ``safe-updates``, ``select_limit``, ``init-command`` or ``force`` there could silently cap or excuse what the tool reads. Put everything the client needs (host, user, password, TLS settings) in the one file you name. Each client is given only its own side's password.

.. note::

   **TLS.** MariaDB client 11.4 and later verifies the server certificate by default. Supply the RDS CA bundle in the option file: ``ssl-ca=/path/to/global-bundle.pem`` (from ``https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem``). Through a port-forward to ``localhost`` the host name will not match the certificate, so there add ``ssl-verify-server-cert=0`` and rely on the tunnel. **With the password in** ``MIG_*_PASSWORD`` **(not in the option file) the client turns verification off by itself** and prints ``WARNING: option --ssl-verify-server-cert is disabled``: for real runs put the password in the option file.

Exit status of every step: ``0`` succeeded; ``1`` a check or comparison found a problem; ``2`` refused, crashed or interrupted (including Ctrl-C, ``kill`` and a closed terminal). **Any non-zero status means stop.** ``run.log`` records each outcome as ``ok``, ``failed``, ``refused``, ``crashed`` or ``interrupted``.

Accounts and grants
~~~~~~~~~~~~~~~~~~~

Without these, information_schema silently hides objects and the tool would see less than there is, so it refuses instead.

- **Source** (read-only use): ``SELECT, SHOW VIEW, TRIGGER, EVENT, EXECUTE`` on the database, and ``PROCESS`` on ``*.*`` for ``preflight --require-quiesced``. Routine bodies must be readable: the account is their definer, or holds ``SHOW_ROUTINE`` (MySQL) / ``SHOW CREATE ROUTINE`` or ``SELECT`` on ``mysql.proc`` (MariaDB). ``preflight`` checks all of this (``source_privileges``).
- **Target**: ``ALL PRIVILEGES`` on the database (restore creates tables, triggers, routines and events), and ``PROCESS`` on ``*.*`` for ``reset-target``, which refuses while other sessions are connected and cannot see them without it.


Rehearsal source
~~~~~~~~~~~~~~~~

The rehearsal copies from a restored copy of the live database, never from the live database itself. Everything stays in the same AWS account: no cross-account copy or share. An administrator does these steps by hand (the test role is denied the live ``awseb-e-*`` database and its snapshots). The copy holds real participant data, so it is restored **private and encrypted, like production**, into the test stack's private subnets.

.. code-block:: bash

   SNAP=rehearsal-source-$(date -u +%Y%m%d-%H%M)
   aws rds create-db-snapshot --db-instance-identifier <old-db-id> --db-snapshot-identifier $SNAP
   aws rds wait db-snapshot-available --db-snapshot-identifier $SNAP
   # A restore keeps the snapshot's encryption and cannot add it. The old
   # database may be unencrypted, so restore from an encrypted copy (for an
   # already encrypted snapshot the copy is harmless).
   aws rds describe-db-snapshots --db-snapshot-identifier $SNAP --query 'DBSnapshots[0].Encrypted'
   aws rds copy-db-snapshot --source-db-snapshot-identifier $SNAP \
     --target-db-snapshot-identifier $SNAP-encrypted --kms-key-id alias/aws/rds
   aws rds wait db-snapshot-available --db-snapshot-identifier $SNAP-encrypted
   aws rds restore-db-instance-from-db-snapshot --db-instance-identifier <PREFIX>-test-rehearsal-source \
     --db-snapshot-identifier $SNAP-encrypted --db-instance-class db.t3.micro --no-publicly-accessible \
     --db-subnet-group-name <PREFIX>-test-database-subnet-group --vpc-security-group-ids <test database security group id> \
     --tags Key=Environment,Value=test Key=Project,Value=<PREFIX>
   aws rds wait db-instance-available --db-instance-identifier <PREFIX>-test-rehearsal-source
   aws rds describe-db-instances --db-instance-identifier <PREFIX>-test-rehearsal-source \
     --query 'DBInstances[0].[StorageEncrypted,PubliclyAccessible]'

*Gate:* the last command prints ``true`` then ``false``. Point ``MIG_SOURCE_*`` at this instance for the rehearsal. The ``<PREFIX>-test-`` name and the ``Environment=test`` tag keep it inside the test role's scope and out of the production role's.

It is not in the test stack's Terraform state, so Destroy Infrastructure does not remove it. Step 13 deletes it by hand.


Procedure
---------

Steps 1 to 3 are preparation and can be done ahead of time. The window (app offline) is steps 4 to 12.

1. **Tag the code.**

   .. code-block:: bash

      git tag -a migration-rehearsal-start -m "Before the data migration rehearsal" <deployed-sha>
      git push origin migration-rehearsal-start

   (``migration-production-start`` for production.) Deploy that tag to the old and the new environment.

   *Gate:* do not continue unless both environments run the tagged commit.

2. **Stand up and check the new stack, then stop its app.** Apply Terraform, deploy, and run ``scripts/smoke.sh <new stack URL>``. Then set the new environment's Auto Scaling group to 0 instances (Elastic Beanstalk console: Configuration, Capacity, min 0 and max 0).

   *Gate:* do not continue unless the smoke test passed and the new environment now has 0 instances. A running app writes to the database it is attached to.

   *Rollback:* none needed; nothing live has changed.

3. **Rehearse the read-only steps against live data.** With the old app still serving::

      python3 scripts/migration/dbcopy.py $RUN preflight --allow-non-empty-target

   It prints both servers' versions and character sets, the size, and every table with its row count. The target is not empty yet (the new app created its tables on first boot); that is expected here.

   *Gate:* do not continue unless every check is ``ok`` or ``warn`` apart from the expected non-empty target. Note the size and use `Timings`_ to confirm the window is long enough.

4. **Take the old app offline. The window starts.** Announce it first. Simplest safe option that keeps an instance available for the port-forward: remove the inbound rule on the old load balancer's security group (note the rule so you can put it back). Alternative: scale the old environment to 0 instances, if the tool reaches the database some other way.

   *Gate:* do not continue unless ``scripts/smoke.sh <old URL>`` now **fails**.

   *Rollback:* restore the security group rule (or scale back up). Nothing else has changed.

5. **Snapshot the old database and prove the snapshot restores.**

   .. code-block:: bash

      SNAP=pre-migration-rehearsal-$(date -u +%Y%m%d-%H%M)
      aws rds create-db-snapshot --db-instance-identifier <old-db-id> --db-snapshot-identifier $SNAP
      aws rds wait db-snapshot-available --db-snapshot-identifier $SNAP
      aws rds restore-db-instance-from-db-snapshot --db-instance-identifier $SNAP-proof \
        --db-snapshot-identifier $SNAP --db-instance-class db.t3.micro --no-publicly-accessible \
        --db-subnet-group-name <old-db-subnet-group> --vpc-security-group-ids <old-db-security-group>
      aws rds wait db-instance-available --db-instance-identifier $SNAP-proof

   Prove it with the same tool: point ``MIG_TARGET_*`` at the ``$SNAP-proof`` endpoint (same user, password and database name as the source) and run, in its own run directory::

      python3 scripts/migration/dbcopy.py --run-dir migration-runs/snapshot-proof verify

   Then delete the proof instance (``aws rds delete-db-instance --db-instance-identifier $SNAP-proof --skip-final-snapshot``), point ``MIG_TARGET_*`` back at the new database, and record the gate::

      python3 scripts/migration/dbcopy.py $RUN snapshot-check --snapshot-id $SNAP --snapshot-verified

   *Gate:* do not continue unless that ``verify`` printed ``IDENTICAL``. ``dump`` refuses to run until ``snapshot-check`` is recorded for this source.

   *Rollback:* as step 4. Keep the snapshot.

6. **Empty the new database.** It holds only what the new app created on first boot. Look at the target row counts in the step 3 report (``preflight-report.json``, ``target.tables``): conversations, utterances, keystrokes and survey responses must be 0, or be test data you recognise. Then, connected to the **new** endpoint (check with ``SELECT @@hostname``)::

      DROP DATABASE chatbot_db; CREATE DATABASE chatbot_db;

   This is deliberately a manual step: the tool only ever empties a database it loaded itself.

   *Gate:* do not continue unless you confirmed the endpoint is the new one before running it.

7. **Preflight, for real.** ::

      python3 scripts/migration/dbcopy.py $RUN preflight --require-quiesced

   *Gate:* do not continue unless it prints ``PREFLIGHT OK``. ``source_quiesced`` failing means something is still connected to or writing to the old database: find it and go back to step 4.

8. **Dump, restore, verify.** ::

      python3 scripts/migration/dbcopy.py $RUN dump
      python3 scripts/migration/dbcopy.py $RUN restore
      python3 scripts/migration/dbcopy.py $RUN verify

   ``dump`` writes ``dump.sql`` (mode 600; it is the whole database in plain text) and its SHA-256, and prints each object's ``DEFINER``. It refuses ``ENABLE``\ d events (disable them on the source; re-enable after cutover), sequences and system-versioned tables, and ``FLOAT`` values its 6-digit text rendering would change (convert those columns to ``DOUBLE`` on the source first). ``restore`` re-checks that checksum on the bytes it feeds the client, refuses a target that is not empty, never replaces an existing table, fails on any server warning, and sets the target database's default collation to the source's.

   *Gate:* do not continue unless ``verify`` printed ``IDENTICAL`` and exited 0. A difference is named by table and key in the output and in ``verify-report.json``.

   *If restore fails or is interrupted:* the source is untouched. Run ``reset-target --confirm <MIG_TARGET_HOST>/chatbot_db`` (host and database exactly as configured), then ``restore`` and ``verify`` again. ``reset-target`` checks by query that the server it reached is the one this run restored into and not the source, and refuses if ``verify`` has passed since the last restore, if the target holds anything the dump did not create, or if other sessions are connected.

   *Rollback:* as step 4. The new database can simply be left.

9. **Copy the uploaded files, under the same keys.** Skip ``uploads/``: it holds participants' raw photos, which the app deletes once their avatar is made and the new bucket expires after 1 day (S3 runs expiry asynchronously and rounds to the next midnight UTC, so 24 to 48 hours in practice). Production's old bucket has the same rule; the staging bucket ``dev.bot-avatar.wwbp.org`` has none and still holds old photos there. Copying them would only carry them over. ::

      OLD=<old-data-bucket>; NEW=<new-data-bucket>
      aws s3 sync s3://$OLD s3://$NEW --exclude 'uploads/*'
      for b in $OLD $NEW; do
        aws s3api list-objects-v2 --bucket $b --output text \
          --query 'Contents[?!starts_with(Key, `uploads/`)].[Key,Size,ETag]' | sort > $b.keys
        wc -l < $b.keys; awk -F'\t' '{s+=$2} END {print s}' $b.keys
      done
      diff $OLD.keys $NEW.keys && echo SAME

   Each line is key, size and ETag. The ETag is the MD5 of the content, except for an object uploaded or copied in parts (over 8 MB), whose ETag contains a ``-`` and can differ between the buckets with the same content: for those lines only, download the object from both buckets and compare ``shasum -a 256``.

   Then list the keys the database refers to that are missing from the new bucket, and those missing from the old one; the two lists must be the same (files that never reached S3)::

      cut -f1 $NEW.keys | sort | comm -23 <(sort -u db.keys) - > missing-new
      cut -f1 $OLD.keys | sort | comm -23 <(sort -u db.keys) - > missing-old
      diff missing-old missing-new && wc -l < missing-new

   *Gate:* do not continue unless ``diff`` prints ``SAME`` (same keys, count, sizes and checksums), or every differing line is a multipart ETag whose SHA-256 matched; and ``diff missing-old missing-new`` prints nothing.

10. **Start the new app.** Set the new environment back to 1 or more instances. Give it the old environment's ``SECRET_KEY`` if staff should stay logged in. From this moment the new database changes (the app records logins and sessions), so ``verify`` will no longer print ``IDENTICAL``: that is why it comes first.

    *Gate:* do not continue unless ``scripts/smoke.sh <new stack URL>`` passes, and you can log in to the admin with an existing account and open a known conversation with its full transcript.

    *Rollback:* scale the new environment to 0, then as step 4.

11. **Switch.** Point the public name (``dev.bot.wwbp.org``, later ``bot.wwbp.org``) at the new stack. Leave the old environment blocked, not terminated.

    *Rollback:* point the name back and restore the old security group rule. **Anything participants wrote to the new database after the switch is not in the old one.** Decide before the window who makes that call and until when; after real participants have written new data, prefer fixing forward.

12. **Check and close the window.** ::

       scripts/smoke.sh https://dev.bot.wwbp.org
       git tag -a migration-rehearsal-done -m "Data migration rehearsal verified" && git push origin migration-rehearsal-done

    *Gate:* smoke test passes on the public name; one test conversation works end to end and appears in the admin.

13. **Rehearsal before production.** Production is not touched until the test-stack rehearsal completed with every gate met and the timings recorded. Then destroy the test stack (Destroy Infrastructure, environment ``test``), and delete the rehearsal source and its snapshots by hand::

       aws rds delete-db-instance --db-instance-identifier <PREFIX>-test-rehearsal-source --skip-final-snapshot --delete-automated-backups
       aws rds delete-db-snapshot --db-snapshot-identifier $SNAP-encrypted
       aws rds delete-db-snapshot --db-snapshot-identifier $SNAP

    Keep production's own step 5 snapshot; delete only the rehearsal's.

14. **Keep the old environment** blocked (or at 0 instances) for 30 days, or one full study cycle if longer. Keep the step 5 snapshot until the study data's retention period ends. Before terminating the old environment, confirm that snapshot still exists and delete ``dump.sql`` from wherever the tool ran.


Rollback at a glance
--------------------

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Point reached
     - Rollback
   * - Before step 4
     - Nothing to undo.
   * - Steps 4 to 10
     - Put the old security group rule back (or scale the old environment up). The old database was never written to.
   * - After step 11, no participant data yet
     - Point the name back; unblock the old environment.
   * - After step 11, with new participant data
     - Rolling back loses that data. Fix forward unless the owner decides otherwise.
   * - Old database lost or damaged at any point
     - Restore the step 5 snapshot (the restore was proven in step 5).


Timings
-------

Measured on a laptop, both servers in containers with 64 MB buffer pools:

.. list-table::
   :header-rows: 1
   :widths: 40 20 20 20

   * - Data
     - dump
     - restore
     - verify
   * - 52,000 rows, 11 MB dump (the test fixture)
     - 0.1 s
     - 0.4 s
     - 0.3 s
   * - 1.6 million rows, 283 MB dump
     - 1.9 s
     - 8.9 s
     - 2.9 s

That is about 150 MB/s dumping, 30 MB/s restoring and 550,000 rows/s verifying, on local disks. Expect a small RDS instance reached through a port-forward to be several times slower, restore most of all. All three scale roughly with data size. The real database's size is not known here: step 3 reports it, and the test-stack rehearsal gives real timings on real hardware and network. RDS operations in step 5 (snapshot, restore to a proof instance) typically take 10 to 25 minutes together regardless of how small the database is, and are the largest part of a 30 to 60 minute window. If the rehearsal shows the window is too tight, the snapshot proof can run in parallel with step 8 (and must still pass before step 11); that is the owner's decision.


What verify does not compare
----------------------------

``verify`` compares: the database's default character set and collation; per table the engine, collation, row format, every column (type, nullability, default, character set, collation, extra, generation expression), every index, foreign key and ``CHECK`` constraint, and ``AUTO_INCREMENT`` (it must be **equal**: the dump carries the counter exactly, so a target that is ahead was written to or copied from a later source; the value compared is the one the next ``INSERT`` would get: ``verify`` opens each table before reading it, because after a server start, such as a snapshot restore, MySQL 8 reports the last value used, one lower, or none, for a table not yet opened); every row, byte for byte (``FLOAT`` widened to ``DOUBLE`` so all its digits count); views by definition, check option and security type; routines by type, name, parameters, return type, body and attributes; triggers; events by body, status and schedule.

It does **not** compare: table, column and index comments; partitioning; ``DEFINER`` accounts (``dump`` reports them); users, grants and server settings; anything outside the database (S3 objects, Redis, environment variables, DNS). It refuses table types other than base tables and views.

Limits
------

- MySQL to MariaDB (or the reverse) is refused.
- Triggers, procedures, events and views are copied, but they carry a ``DEFINER``: they load only if the same account name exists on the target (or the restoring account holds ``SET_USER_ID`` / ``SUPER``). ``dump`` lists each definer. The app defines none.
- ``--require-quiesced`` samples the source for a few seconds; it is a check, not a lock. ``verify`` is the proof: it fails if the source changed after the dump.
- The dump is uncompressed and needs free disk of roughly the database's data size.
- S3 objects, Redis, environment variables and DNS are outside the tool.
