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
                 RDS MariaDB      ElastiCache
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
     - MariaDB 10.11, ``db.t3.micro`` default, 20–100 GB gp3, private subnets only
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

``deploy-infrastructure.yml`` (plan on pull requests, apply by hand)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The workflow has two jobs. Setup is in ``infra/terraform/README.md``, section "Running Terraform from GitHub Actions".

**plan** runs on each pull request that changes ``infra/terraform/``, once a reviewer approves the run in the ``plan`` GitHub Environment. It runs ``terraform plan`` for staging and for production with a read-only IAM role that trusts only that Environment. It takes no state lock and gets no application secrets, but the role can read the state, which still holds secrets: approving a plan lets the pull request's code read them. If the role is not configured, the job skips with a notice.

**apply** runs only when a person starts the workflow. The person selects ``environment`` (``staging`` or ``production``) and ``action`` (``plan`` or ``apply``; the default is ``plan``). The job runs in the GitHub Environment with the same name as ``environment``. Required reviewers on that Environment must approve the run before it starts. Steps:

1. **Check required secrets**. Runs before the job touches AWS.
2. **AWS credentials**. Assumes the role in ``TF_APPLY_ROLE_ARN`` through GitHub OIDC. The workflow uses no long-lived keys.
3. **State bucket**. Finds ``{PROJECT_PREFIX}-tfstate-{ACCOUNT_ID}``. Creates it on a first apply.
4. **Terraform init**. Points to the state file of the selected environment. A second run on the same environment stops with a lock error.
5. **ACM certificate** (apply with a custom domain only). Targeted apply, so the validation CNAME can be printed before the full apply blocks waiting for DNS.
6. **Plan**. Reads ``environments/{environment}.tfvars`` and prints the plan. With ``action = plan`` the job stops here.
7. **Apply**. Applies the plan from step 6 and nothing else.
8. **Write deployment secrets** (only when the ``write_deploy_secrets`` input is ticked). Uses ``GH_PAT`` to write the deploy secrets of the selected environment with ``gh secret set``. Staging gets the names ``staging.yml`` reads (``AWS_ROLE_NAME``, ``AWS_S3_BUCKET_NAME_FRONTEND``, etc.). Production gets the names ``production.yml`` reads (``AWS_PROD_ROLE_NAME``, ``AWS_S3_PROD_BUCKET_NAME_FRONTEND``, etc.). The two lists share no name. The workflow does not start an application deploy. Writing staging's names first requires the three ``AWS_PROD_*`` secrets production falls back from to exist; otherwise the run stops.

   **Before ticking it for production**, write down the values production deploys use today (account ID, role, frontend bucket, CloudFront distribution, EB application and environment; read them from the AWS console, since GitHub never shows a secret's value once set). Overwriting the ``AWS_PROD_*`` secrets is the cutover, and those notes are the only way back.
9. **Report failure**. If any step fails, the workflow writes a notice to the Summary and changes nothing. It does not destroy, delete or import resources.
10. **Summary**. Writes the chatbot URL and admin panel URL to the workflow Summary.

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
- Subject: ``repo:{github_org}/{github_repo}:*`` (scoped to the exact fork)

``deploy-infrastructure.yml`` also signs in through OIDC, with roles that a person creates one time from the policies in ``infra/terraform/bootstrap/iam/``: a read-only role for the plan job and one apply role per environment. Each carries an explicit Deny on the hand-built live resources in the same account. ``destroy-infrastructure.yml`` still uses the long-lived ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY``.


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

1. RDS automated backups are disabled by default
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``backup_retention_period = 0`` in ``infra/terraform/rds.tf``. This was intentional: some AWS free-tier and newly created accounts reject RDS instances with ``backup_retention_period > 0`` during certain multi-AZ or instance class combinations, causing ``terraform apply`` to fail. With retention at 0, deployment works on any account type.

**What you lose:** point-in-time recovery. If the database is corrupted or data is accidentally deleted, you cannot restore to a previous state.

**To enable backups:** change ``backup_retention_period`` to ``7`` (or higher) in ``rds.tf`` and re-run the Deploy Infrastructure workflow. AWS will begin taking daily snapshots retained for that many days. Note that backup storage incurs a small additional cost.

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

**Fix:** Set the GitHub secret ``CREATE_GITHUB_OIDC_PROVIDER`` to ``false``. The workflow passes this to ``TF_VAR_create_github_oidc_provider``, which skips the OIDC provider resource and instead imports the existing one.

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

**Fix:** generate a new token, update the ``GH_PAT`` secret in GitHub Settings, and re-run the workflow.

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
