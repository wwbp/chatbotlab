Technical Reference
===================

This document covers the architecture, deployment mechanics, and known edge cases for the ChatbotLab AWS deployment. It is intended for engineers who need to understand, debug, or modify the deployment system.


Architecture overview
---------------------

.. code-block:: text

   Participants/Researchers
           │
           ▼
      CloudFront CDN
      (xxxx.cloudfront.net)
           │
      ┌────┴──────────────────┐
      │                       │
      ▼                       ▼
   S3 bucket              Elastic Beanstalk
   (React SPA)            (Django + nginx)
                               │
                      ┌────────┴────────┐
                      ▼                 ▼
                 RDS MySQL        ElastiCache
                 (private subnet) Serverless Redis
                                  (private subnet)

**Traffic routing via CloudFront:**

- ``/api/*`` → Elastic Beanstalk (Django REST API, no caching)
- ``/ws/*`` → Elastic Beanstalk (Django Channels WebSocket, no caching)
- ``/static/admin/*`` → Elastic Beanstalk (Django admin CSS/JS via WhiteNoise)
- Everything else → S3 (React SPA, cached aggressively)

**SPA routing:** CloudFront maps S3 404s (returned for unknown paths like ``/dashboard``) to HTTP 200 with ``index.html``, so React Router handles all client-side navigation. 403s are intentionally **not** caught. They represent real Django errors (CSRF failures, permission denied) and should surface to the client.


AWS resources provisioned
--------------------------

All resources are provisioned by Terraform in ``infra/terraform/``. The name prefix for all
resources is ``{PROJECT_PREFIX}-production``, where ``{PROJECT_PREFIX}`` is the value of
``var.project_name`` (supplied to the workflows via the ``TF_PROJECT_PREFIX`` secret).

.. list-table::
   :header-rows: 1
   :widths: 25 30 45

   * - Resource
     - Type
     - Notes
   * - VPC
     - ``aws_vpc``
     - 10.0.0.0/16, 2 public + 2 private subnets across 2 AZs
   * - Elastic Beanstalk application
     - ``aws_elastic_beanstalk_application``
     - Docker on Amazon Linux 2023
   * - Elastic Beanstalk environment
     - ``aws_elastic_beanstalk_environment``
     - ALB, auto-scaling, EC2 in public subnets
   * - RDS
     - ``aws_db_instance``
     - MySQL 8.4, ``db.t3.micro`` default, 20–100 GB gp3, private subnets only
   * - ElastiCache
     - ``aws_elasticache_serverless_cache``
     - Serverless Redis with TLS (``rediss://``), private subnets only
   * - S3 (frontend)
     - ``aws_s3_bucket``
     - React build artifacts, served by CloudFront via OAC
   * - S3 (data)
     - ``aws_s3_bucket``
     - Media uploads, voice audio, avatar images
   * - CloudFront
     - ``aws_cloudfront_distribution``
     - PriceClass_100 (US/Canada/Europe), OAC for S3 origin
   * - ACM certificate
     - ``aws_acm_certificate``
     - Only when ``domain_name`` is set in ``environments/{environment}.tfvars``; must be in ``us-east-1``
   * - IAM role (EB instance)
     - ``aws_iam_role``
     - Allows EC2 to read S3, write CloudWatch logs
   * - IAM role (EB service)
     - ``aws_iam_role``
     - EB control plane role
   * - IAM role (GitHub Actions)
     - ``aws_iam_role``
     - OIDC trust, scoped to specific repo
   * - GitHub OIDC provider
     - ``aws_iam_openid_connect_provider``
     - One per AWS account. See gotcha below.
   * - Terraform state bucket
     - (created by workflow script)
     - ``{PROJECT_PREFIX}-tfstate-{ACCOUNT_ID}``, versioned, AES-256 encrypted. One state file per environment: ``{PROJECT_PREFIX}/{environment}/terraform.tfstate``, locked with an S3 lock file


Deployment workflows
---------------------

``deploy-infrastructure.yml`` (manual trigger only)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Pull requests never run it and get no AWS access: they run only the offline checks in ``ci.yml`` (job **Terraform test**: ``terraform fmt -check``, ``terraform validate``, the mocked ``terraform test`` and ``check_workflows.py``). Setup is in ``infra/terraform/README.md``, section "Running Terraform from GitHub Actions".

The one job runs only when a person starts the workflow. The person selects ``environment`` (``test``, the on-demand stack, or ``production``; both live in the same AWS account) and ``action`` (``plan`` or ``apply``; the default is ``plan``). The job runs in the GitHub Environment with the same name as ``environment``. Required reviewers on that Environment must approve the run before it starts. Steps:

1. **Check required secrets**. Runs before the job touches AWS. Production runs only from ``main``.
2. **AWS credentials**. Signs in to the one account, ``TF_AWS_ACCOUNT_ID``, as the role of the environment (``<PREFIX>-terraform-apply-production`` or ``<PREFIX>-terraform-apply-test``) through GitHub OIDC, and refuses credentials from any other account. The workflow uses no long-lived keys.
3. **State bucket**. Finds ``{PROJECT_PREFIX}-tfstate-{ACCOUNT_ID}`` in that account. Creates it on a first apply.
4. **Terraform init**. Points to the state file of the selected environment. A second run on the same environment waits for the first (concurrency group ``terraform-<environment>``); a run started outside this workflow stops with a lock error.
5. **ACM certificate** (apply with a custom domain only). Targeted apply, so the validation CNAME can be printed before the full apply blocks waiting for DNS. It runs before the plan in step 6 and is not part of it; it touches only the certificate.
6. **Plan**. Reads ``environments/{environment}.tfvars`` and prints the plan. With ``action = plan`` the job stops here.
7. **Check the plan for deletions** (apply only). If the plan deletes or replaces any resource, the run stops unless ``allow_destroy`` was ticked.
8. **Apply**. Applies the plan saved in step 6.
9. **Write deployment secrets** (production only, only when the ``write_deploy_secrets`` input is ticked). Uses ``GH_PAT`` to write the names ``production.yml`` reads (``AWS_PROD_ROLE_NAME``, ``AWS_S3_PROD_BUCKET_NAME_FRONTEND``, etc.) with ``gh secret set``. It never writes the plain ``AWS_*`` names ``staging.yml`` reads. The workflow does not start an application deploy.

   **Before ticking it**, write down the values production deploys use today (account ID, role, frontend bucket, CloudFront distribution, EB application and environment; read them from the AWS console, since GitHub never shows a secret's value once set). Overwriting the ``AWS_PROD_*`` secrets is the cutover, and those notes are the only way back.
10. **Report failure**. If any step fails, the workflow writes a notice to the Summary and changes nothing. It does not destroy, delete or import resources.
11. **Summary**. Writes the chatbot URL and admin panel URL to the workflow Summary.

``destroy-infrastructure.yml`` (manual trigger only)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Deletes one environment. A first job checks the typed values before the workflow signs in to AWS; the second runs in the GitHub Environment with the same name, so required reviewers must approve it.

- **test**: the person types ``test``. Nothing else is asked and **nothing is backed up**: the test stack's data bucket and database are deleted for good.
- **production**: the person types ``production``, ``destroy production`` and ``destroy data``, ticks ``allow_production``, and runs it from ``main``. The job first copies the data bucket to ``<state bucket>/<prefix>/production/backups/<UTC time>/data/`` and compares object count and total size, stopping on a mismatch before changing anything. Then it turns off RDS deletion protection and waits for that to apply, empties the buckets and runs ``terraform destroy``. If the run fails after it turned deletion protection off, a last step turns it back on. The database leaves a final snapshot. The run summary names the backup and the snapshot.

It does not delete the Terraform state bucket. The GitHub OIDC provider is never deleted.

``production.yml`` (triggers on push to ``main`` or ``workflow_dispatch``)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

1. Runs the CI suite (``ci.yml``) as a prerequisite
2. **Frontend:** builds React with ``VITE_API_URL`` injected, syncs to S3, invalidates CloudFront cache
3. **Backend:** runs ``eb deploy`` via the AWS EB CLI, which packages and uploads the Docker image and triggers a rolling deployment on EB
4. **Smoke test:** once both deploys finish, runs ``scripts/smoke.sh <site-url>``, which polls ``/api/health/ready/`` for up to five minutes and then checks that ``/`` serves the app. The workflow fails if either check does not pass. It reports a broken deploy; it does not roll it back. ``staging.yml`` does the same.

The site URL is ``https://bot.wwbp.org`` (``https://dev.bot.wwbp.org`` for staging) in the ``wwbp/chatbotlab`` repository. Any other deployment sets the repository variable ``PRODUCTION_SITE_URL`` (``STAGING_SITE_URL``) to its own URL, for example ``https://xxxx.cloudfront.net``; without it the smoke test is skipped with a warning.


Health endpoints
-----------------

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Path
     - Purpose
   * - ``/health/``
     - Liveness. Returns 200 whenever Django is running and checks nothing else. This is the Elastic Beanstalk / load balancer health check path, so it must not depend on the database: a brief database blip would mark every instance unhealthy and trigger replacements. CloudFront does not forward it, so it is not reachable from the internet.
   * - ``/api/health/ready/``
     - Readiness. Runs ``SELECT 1`` on the database and a set/get on the Redis cache. Returns 200 ``{"status": "ok", ...}`` when both work, otherwise 503 with ``"database"`` or ``"cache"`` marked ``"failed"``. The cause is in the application log, never in the response. Public and unauthenticated, under ``/api/`` so that it is reachable through CloudFront.

Unapplied migrations are not checked: ``entrypoint.sh`` runs ``migrate`` under ``set -e`` before the server starts, so a container whose migrations failed never serves requests.

Run the smoke test by hand at any time::

    scripts/smoke.sh https://dev.bot.wwbp.org


GitHub OIDC (passwordless CI/CD)
---------------------------------

After infrastructure is provisioned, GitHub Actions assumes an IAM role via OIDC. No long-lived keys are stored in secrets for ongoing deployments.

The trust policy on the GitHub Actions IAM role restricts assumption to:

- OIDC issuer: ``token.actions.githubusercontent.com``
- Subject, exactly (no wildcard): ``repo:{github_org}/{github_repo}:ref:refs/heads/main`` for production (``production.yml`` on ``main``), ``repo:{github_org}/{github_repo}:environment:test`` for the test stack; each also in the immutable-ID form ``repo:{org}@{org_id}/{repo}@{repo_id}:...``. A workflow a pull request adds cannot assume it.

``deploy-infrastructure.yml`` and ``destroy-infrastructure.yml`` also sign in through OIDC, with an apply role per environment that a person creates one time from the policies in ``infra/terraform/bootstrap/iam/``: ``<PREFIX>-terraform-apply-production`` and ``<PREFIX>-terraform-apply-test``, both in the same AWS account (``TF_AWS_ACCOUNT_ID``), which also holds the hand-built live deployment and other lab projects. Each has a permissions boundary for the roles its stack creates. Each is allowed only its own ``<PREFIX>-<environment>-*`` names and ``Environment``/``Project`` tags (which keeps other projects out), and explicitly denied the other environment's names, tags and state key, the hand-built live resources, and any change to the GitHub OIDC provider. The test role and the test stack's roles are also denied ``ses:Send*``: the test stack sends no real email. See ``infra/terraform/README.md``, "One account, two roles". No workflow uses long-lived AWS keys.


Application stack
-----------------

**Container layout:**

.. code-block:: text

   nginx (port 80) → gunicorn with uvicorn workers (port 8000)

- nginx handles static files (``/static/``, ``/media/``) directly from the filesystem and proxies everything else to gunicorn
- gunicorn uses ``--worker-class uvicorn.workers.UvicornWorker`` for ASGI support
- Django Channels ``ProtocolTypeRouter`` routes HTTP to ``get_asgi_application()`` and WebSocket to ``AuthMiddlewareStack(URLRouter(websocket_urlpatterns))``

**nginx sets ``X-Forwarded-Proto: https`` unconditionally** (hardcoded, not proxied from the incoming header). This tells Django's ``SECURE_PROXY_SSL_HEADER`` check that the connection is HTTPS even though CloudFront → EB traffic travels over plain HTTP on the AWS backbone.

**Database connection pooling is disabled (``CONN_MAX_AGE = 0``).** Django's async (ASGI) stack with MySQL is thread-sensitive: all ORM calls are serialized through a single shared thread per worker. Reusing connections across requests causes state corruption under concurrent load. See the comment in ``api/generic_chatbot/settings.py`` for details.


Environment variables
---------------------

Terraform sets the application's environment variables on the Elastic Beanstalk environment (``infra/terraform/elasticbeanstalk.tf``). ``api/env_vars.txt`` lists every variable the application reads; a test keeps the list in step with the code, and ``infra/terraform/tests/env_parity.tftest.hcl`` fails if production leaves one out without a reason.

.. list-table:: Set by Terraform
   :header-rows: 1
   :widths: 30 35 35

   * - Variable
     - Production value
     - Notes
   * - ``DEBUG``
     - ``False``
     -
   * - ``BACKEND_ENVIRONMENT``
     - ``production`` (``test`` on the test stack)
     -
   * - ``APP_SECRET_ID``
     - ``<prefix>-production/app``
     - The name of the application secret, not a value (see `Secrets`_)
   * - ``ALLOWED_HOSTS``
     - ``*``
     -
   * - ``CORS_ALLOWED_ORIGINS``, ``CSRF_TRUSTED_ORIGINS``
     - ``https://bot.wwbp.org,https://*.cloudfront.net``
     - Also ``https://<domain_name>`` when set
   * - ``SITE_URL``
     - ``https://bot.wwbp.org``
     - ``site_url`` in ``environments/production.tfvars``; links in emails
   * - ``EMAIL_BACKEND``
     - ``chatbot.email_backend.SESBackend``
     - Test stack: ``django.core.mail.backends.console.EmailBackend`` (the log), and nothing else
   * - ``DEFAULT_FROM_EMAIL``
     - ``ChatbotLab <no-reply@bot.wwbp.org>``
     - The instance role may send as this address only
   * - ``DATABASE_HOST``, ``DATABASE_PORT``, ``DATABASE_NAME``
     - the Terraform RDS instance
     -
   * - ``DATABASE_SECRET_ID``
     - ARN of ``rds!db-<id>``
     - The secret RDS keeps the master user in; the application reads ``DATABASE_USER`` and ``DATABASE_PASSWORD`` from it
   * - ``CONN_MAX_AGE``
     - ``0``
     - See gotcha 7
   * - ``REDIS_URL``
     - the Terraform ElastiCache, ``rediss://``
     -
   * - ``AWS_BUCKET_NAME``
     - the Terraform data bucket
     - Uploads and avatars
   * - ``AWS_REGION``
     - ``aws_region``
     -
   * - ``GUNICORN_WORKERS``
     - ``4``
     - Async workers, two per vCPU of a ``t3.small``. Database connections follow concurrent requests, not workers (``CONN_MAX_AGE = 0``)
   * - ``MOCK_LLM``
     - ``false``
     - Load-test switch, pinned off
   * - ``DJANGO_SUPERUSER_USERNAME``, ``_EMAIL``, ``_PASSWORD``
     - not set
     - Test stack only: ``admin``, ``admin@example.com``, ``admin_panel_password``. ``api/entrypoint.sh`` tries to create this user on every boot while the password is set

**Read by the application, not set on purpose** (the reasons are in ``api/env_vars.txt``): ``AWS_ACCESS_KEY_ID`` and ``AWS_SECRET_ACCESS_KEY`` (the instance role gives AWS access; static keys would override it), ``MOCK_LLM_P50_MS`` and ``MOCK_MODERATION_P50_MS`` (only with ``MOCK_LLM`` on), ``DJANGO_SUPERUSER_*`` (production creates no account at boot; its copied database has the real admins; run ``manage.py createsuperuser`` once if ever needed), ``CHATBOT_AVATAR_PROMPT`` and ``CHATBOT_CONTROL_IMAGE`` (live production does not set them either), ``FRONTEND_URL``, ``MODERATION_VALUES_FOR_BLOCKED``, ``DJANGO_SETTINGS_MODULE``; and the secrets, ``SECRET_KEY``, ``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``, ``SURVEY_INGEST_TOKEN``, ``DATABASE_USER`` and ``DATABASE_PASSWORD``, which come from Secrets Manager (`Secrets`_).

**Set on the hand-built environments, dropped on purpose** because no code reads them: ``BASE_URL`` (``SITE_URL`` replaced it), ``CORS_ALLOW_CREDENTIALS``, ``REACT_APP_API_URL`` (the front end reads ``VITE_API_URL`` at build time). Terraform no longer sets ``DATABASE_ENGINE`` for the same reason.


Secrets
-------

No secret value is in Terraform state, the plan, a workflow or the Elastic Beanstalk configuration. The environment gets only the secrets' names, and ``api/generic_chatbot/aws_secrets.py`` reads the values once per process at start, with the instance role:

- ``APP_SECRET_ID`` names ``<prefix>-<environment>/app``, one JSON object with ``SECRET_KEY`` (required) and, when used, ``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY`` and ``SURVEY_INGEST_TOKEN``. Any other key stops the app (a typo would otherwise switch a provider off silently).
- ``DATABASE_SECRET_ID`` names the secret RDS keeps the master user in (``manage_master_user_password`` in ``rds.tf``). RDS rotates it every 7 days and the old password stops working at once; the app's MySQL backend (``api/generic_chatbot/db/base.py``) reads the secret again when a connection is refused with error 1045.

When a name is set, its secret is required: a missing secret or value, or no permission, stops the app at start (the deploy then fails its health check). Without the names (local development, CI, and the hand-built live environment until the cutover) the same variables are read from the environment.

**Who can do what.** An administrator creates and fills ``<prefix>-<environment>/app``. The Terraform apply roles may only describe it, and are explicitly denied every call that writes, restores, deletes or reads a value (``bootstrap/iam/apply-*-guardrails.json``, ``DenySecretValueCalls``): ``CreateSecret`` and ``UpdateSecret`` take a value, so a workflow allowed them could replace ``SECRET_KEY`` and forge staff sessions. They may create and tag ``rds!db-*`` only through RDS (``aws:ViaAWSService``). The instance role may read ``<prefix>-<environment>/*`` and its own database's RDS secret, nothing else (``iam.tf``; the permissions boundary allows no more). The secret is not part of the stack, so Destroy Infrastructure leaves it, and the test stack keeps its keys between rehearsals.

**The test stack uses its own keys**: ``<prefix>-test/app`` holds its own ``SECRET_KEY`` and LLM keys scoped to it (a separate project or key with a low spending limit), never production's.

Create and fill the secret (once per environment, before its first apply; the plan fails until it exists). As an administrator, in bash, write the values to a file only you can read, outside the repository, so they never appear on a command line or in shell history. Generate a new ``SECRET_KEY`` with ``python3 -c 'import secrets; print(secrets.token_urlsafe(50))'`` (production's comes from the live environment instead: runbook, step 10)::

    umask 077
    cat > ~/app-secret.json    # paste the JSON below with real values, then Ctrl-D
    {"SECRET_KEY": "...", "OPENAI_API_KEY": "...", "ANTHROPIC_API_KEY": "...", "SURVEY_INGEST_TOKEN": "..."}

    aws secretsmanager create-secret --name "<prefix>-<environment>/app" \
      --description "ChatbotLab application secrets (JSON)" \
      --tags Key=Project,Value=<prefix> Key=Environment,Value=<environment> \
      --secret-string file://$HOME/app-secret.json
    rm ~/app-secret.json

Leave out the keys a deployment does not use (no ``SURVEY_INGEST_TOKEN`` means the survey endpoint refuses everything).

Rotate a key (or change any value): rewrite the JSON without printing it, then restart the app so every process reads it again::

    read -rs NEW_VALUE && export NEW_VALUE    # type the new key, Enter
    aws secretsmanager get-secret-value --secret-id "<prefix>-<environment>/app" \
        --query SecretString --output text \
      | jq -c '.OPENAI_API_KEY = env.NEW_VALUE' \
      | aws secretsmanager put-secret-value --secret-id "<prefix>-<environment>/app" \
          --secret-string file:///dev/stdin
    unset NEW_VALUE
    aws elasticbeanstalk restart-app-server --environment-name <prefix>-<environment>-env

A new ``SECRET_KEY`` signs everyone out and invalidates reset links. The database password needs nothing: ``aws rds modify-db-instance --db-instance-identifier <prefix>-<environment>-database --rotate-master-user-password --apply-immediately`` rotates it now, and the app follows.

Before this change, Terraform state held the database password, a generated ``SECRET_KEY`` and the API keys, and the state bucket keeps old versions. After the first apply with this change, rotate the OpenAI, Anthropic and survey keys that were passed as variables (the database password is replaced by RDS, and production's ``SECRET_KEY`` comes from the live environment, see the runbook, step 10).


Security configuration
-----------------------

.. list-table::
   :header-rows: 1
   :widths: 28 30 42

   * - Setting
     - Value
     - Reason
   * - ``CSRF_TRUSTED_ORIGINS``
     - Includes ``https://*.cloudfront.net``
     - Django 4.0+ wildcard; avoids Terraform cycle between EB and CloudFront
   * - ``CORS_ALLOWED_ORIGIN_REGEXES``
     - ``^https://[a-z0-9-]+\.cloudfront\.net$``
     - ``CORS_ALLOWED_ORIGINS`` does not support wildcards. The regex list does.
   * - ``SESSION_COOKIE_SAMESITE``
     - ``Lax``
     - Prevents CSRF via cross-site requests while still working with top-level navigation
   * - ``CSRF_COOKIE_SAMESITE``
     - ``Lax``
     - Same
   * - ``SESSION_COOKIE_SECURE``
     - ``True``
     - Cookies only sent over HTTPS
   * - ``SECURE_PROXY_SSL_HEADER``
     - ``("HTTP_X_FORWARDED_PROTO", "https")``
     - Trust nginx's hardcoded header to detect HTTPS
   * - ``ALLOWED_HOSTS``
     - ``.cloudfront.net`` (wildcard) + custom domain if set
     - Avoids referencing CloudFront resource from EB resource (cycle)
   * - S3 frontend bucket
     - Private, OAC only
     - No public access; CloudFront authenticates with SigV4
   * - RDS
     - Private subnets only, ``publicly_accessible = false``
     - Not reachable from the internet
   * - ElastiCache
     - Private subnets only, TLS required
     - ``rediss://`` URL enforces TLS


Known gotchas
-------------

1. RDS automated backups depend on the environment
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``backup_retention_period`` is ``7`` days for production (``environments/production.tfvars``; ``variables.tf`` refuses fewer) and ``1`` day for the test stack. AWS takes a daily snapshot and keeps the transaction logs for that period, so you can restore the database to a point in time in that period.

Production also has ``deletion_protection`` and takes a final snapshot when the database is deleted. The test stack has neither, and its buckets may be force-destroyed.

**To keep backups longer:** change the number in ``environments/production.tfvars`` in a pull request and apply it with the Deploy Infrastructure workflow. Backup storage has a small cost.

2. CloudFront ``custom_error_response`` blocks are global
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

CloudFront error response mappings apply to **all origins**, both S3 and EB. The original configuration had a ``403 → 200 index.html`` mapping intended for S3 SPA routing. This silently intercepted Django's real 403 responses (CSRF failures, permission denied) and served the React app instead. The admin login appeared as a blank white page with no error.

S3 with OAC returns **404** (not 403) for missing objects (``NoSuchKey``). The 403 mapping is therefore both unnecessary and harmful. The current configuration only catches 404 (for SPA routing) and lets all 403s pass through to the client.

**If you add a ``custom_error_response`` for 403 in the future:** be aware it will also swallow Django's 403 responses.

3. Terraform dependency cycle between EB and CloudFront
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

CloudFront references ``aws_elastic_beanstalk_environment.chatbot_api.cname`` (its origin). EB cannot reference ``aws_cloudfront_distribution.frontend.domain_name`` in return. Doing so creates a cycle that Terraform cannot resolve.

**Workaround in use:** ``ALLOWED_HOSTS`` uses ``.cloudfront.net`` (leading dot = Django subdomain wildcard) so it matches any CloudFront domain without referencing the specific distribution. ``FRONTEND_URL`` is only set when ``var.domain_name`` is provided (a static string), not derived from the CloudFront resource. ``CSRF_TRUSTED_ORIGINS`` includes ``https://*.cloudfront.net`` statically in ``settings.py``.

4. A failed apply is not rolled back
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If ``terraform apply`` fails, the resources it already created stay in AWS and in the Terraform state. The workflow used to run ``terraform destroy`` on any failure, and to delete the database when the state looked wrong. On a re-run against a stack that holds study data, that deleted the stack. Both steps are removed.

**What to do:** read the failed step's log, fix the cause and run the workflow again. Terraform continues from the state. To remove a stack on purpose, run the Destroy Infrastructure workflow.

**If a resource exists in AWS but not in the state** (``AlreadyExists`` errors), do not delete it from a script. Find out which state owns it first, then run ``terraform import`` or remove the resource by hand.

5. GitHub OIDC provider is a singleton per AWS account
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Each AWS account can have exactly one OIDC provider for ``token.actions.githubusercontent.com``. If your account already has one (from another project), Terraform's attempt to create a second one will fail.

**Fix:** The workflows always pass ``TF_VAR_create_github_oidc_provider=false``: Terraform refers to the existing provider and never owns it, so destroying a stack cannot delete it. Create the provider once, by hand, if the account has none (``infra/terraform/README.md``, "One-time setup", step 1). The Terraform roles cannot change it.

To check whether your account already has a GitHub OIDC provider:

.. code-block:: bash

   aws iam list-open-id-connect-providers

6. EB launch configurations are deprecated (post-October 2024)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

AWS stopped allowing new launch configurations on accounts created after October 2024. Elastic Beanstalk previously used launch configurations to define EC2 instance settings. Setting ``DisableIMDSv1 = true`` in the EB environment configuration signals EB to use a launch template instead. Without this flag, EB silently attempts to create a launch configuration and fails on newer accounts.

This is already set in ``infra/terraform/elasticbeanstalk.tf`` and requires no action.

7. ``CONN_MAX_AGE > 0`` breaks Django ASGI with MySQL
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Django's async (ASGI) request handling uses ``thread_sensitive=True`` for database operations, which serializes all ORM calls through a single thread per worker. When connection pooling is enabled (``CONN_MAX_AGE > 0``), the same connection object is reused across concurrent async requests routed through that shared thread, causing state corruption. At 5+ concurrent requests, this produced ~80% error rates in testing.

``CONN_MAX_AGE = 0`` is set in ``settings.py``. Each request opens and closes its own connection. ``CONN_HEALTH_CHECKS = True`` handles the "server has gone away" error that can occur when a new connection hits a briefly-unavailable database.

**Long-term fix:** migrate to ``aiomysql``/``asyncmy`` (native async MySQL drivers) or add ProxySQL for connection pooling outside Django.

8. GH_PAT expires
~~~~~~~~~~~~~~~~~~

The GitHub Personal Access Token has an expiration date. When it expires, the Deploy Infrastructure workflow will fail at the "Write deployment secrets" step.

**Fix:** generate a new token, update the ``GH_PAT`` secret of the ``production`` GitHub Environment (an Environment secret, not a repository secret), and re-run the workflow.

**Note:** expiry only affects re-running the infra workflow. Day-to-day application deploys (push to ``main``) use the OIDC IAM role and are not affected by GH_PAT expiry.

9. Custom domain requires two workflow runs (first time only)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

On the first deploy with a custom domain:

1. **Run 1:** Terraform creates the ACM certificate and prints the DNS validation CNAME to the workflow Summary. The researcher adds the CNAME at their registrar. Terraform waits up to 30 minutes for DNS to propagate. If propagation completes in time, the rest of the infrastructure is provisioned in the same run. If not, the workflow times out.
2. **Run 2 (if needed):** Re-run Deploy Infrastructure. DNS is already propagated, so Terraform validates the certificate immediately and continues.

On subsequent re-runs with the same custom domain, the certificate already exists and is validated. No DNS action is required.

10. CloudFront serves US/Canada/Europe only by default
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The Terraform configuration uses ``PriceClass_100``, which distributes content from CloudFront edge locations in the US, Canada, and Europe only. Participants in other regions will still receive correct responses but may see higher latency.

**To serve globally:** change ``price_class = "PriceClass_All"`` in ``infra/terraform/cloudfront.tf`` and re-run Deploy Infrastructure. This increases CloudFront costs slightly.


Re-running the infrastructure workflow
---------------------------------------

The workflow is safe to re-run at any time. Terraform is idempotent. It compares the desired state (in ``.tf`` files) against the current state (in S3) and only changes what is different. Re-running to change instance sizes, rotate secrets, or add a custom domain is the intended use pattern.

A failed run leaves its resources in place and in the state (see gotcha 4). Run the workflow again after you fix the cause.
