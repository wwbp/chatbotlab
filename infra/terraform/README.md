# ChatbotLab — Infrastructure as Code

This directory contains the Terraform configuration that provisions the complete
AWS infrastructure for the ChatbotLab application. Running `bash setup.sh`
creates everything from scratch: networking, database, cache, storage, CDN, and
application server. All environment variables are wired automatically — there is
nothing to configure in the AWS console after apply completes.

> **The repository name and the AWS resource prefix are not the same thing**
>
> The GitHub repository was renamed to `chatbotlab`, but the deployed AWS
> resources were deliberately **not** renamed. `var.project_name` still holds
> the original prefix, because it names live S3 buckets, the RDS instance, the
> Elastic Beanstalk application and the Terraform state bucket. Changing it
> would force Terraform to recreate every resource, **destroying the production
> database**.
>
> Only `var.github_repo` tracks the repository name. Throughout these docs the
> prefix is written as `<project-prefix>`; the real value is supplied to the
> workflows through the `TF_PROJECT_PREFIX` secret and lives in
> `var.project_name`. Do not "finish" the rename by editing it.

> **This Terraform does not manage the live deployment**
>
> The live sites (production `bot.wwbp.org`, and the dev site
> `dev.bot.wwbp.org` that `staging.yml` deploys) were built **manually**, before this IaC existed, and
> are not tracked in any Terraform state. As of this writing the live stack is:
>
> | Layer | Live (manual, unmanaged) | What Terraform would create |
> |---|---|---|
> | Frontend | S3 `bot.wwbp.org` / `dev.bot.wwbp.org` + CloudFront | `<prefix>-<env>-frontend` |
> | Backend | EB application `humanlikebot` | EB application `<prefix>-<env>` |
> | Database | EB-managed `awseb-e-*-stack-awsebrdsdatabase-*` — **real study data** | `<prefix>-<env>-database` |
>
> The two name sets do not overlap, so `terraform apply` will **not** adopt or
> modify the live deployment — it will stand up a *second*, parallel stack that
> you also pay for. Migrating the live deployment under Terraform is a separate
> exercise requiring deliberate `terraform import` of each resource.
>
> To validate the IaC, build the on-demand **test stack** and destroy it
> afterwards (see "Running Terraform from GitHub Actions"). It lives in the
> same AWS account as production and the live deployment, isolated by name,
> tags and explicit Denies.

---

## What gets created

| Resource | Type | Purpose |
|---|---|---|
| VPC | 10.0.0.0/16 across 2 AZs | Private network — nothing reaches the DB or cache except the app |
| Public subnets | 10.0.1/24, 10.0.2/24 | Load balancer and application instances |
| Private subnets | 10.0.10/24, 10.0.11/24 | RDS and ElastiCache — no internet route |
| Security groups | 4 (CloudFront → ALB → EB → RDS/Cache) | Least-privilege firewall chain; the ALB accepts CloudFront only |
| RDS MySQL 8.4 | db.t3.micro | Primary database, encrypted, automated backups |
| ElastiCache Serverless Redis | TLS-only | Django session cache |
| S3 frontend bucket | Private + CloudFront OAC | Compiled React app |
| S3 data bucket | Private + CORS | Avatars, uploads, voice audio |
| CloudFront distribution | OAC, SPA routing | CDN for the React app |
| Elastic Beanstalk | Docker on AL2023, ALB | Django API server |
| IAM instance role | Least privilege | App reads/writes S3, calls Bedrock — no hardcoded keys |
| IAM GitHub OIDC role | Scoped to this repo | GitHub Actions deploys without storing AWS keys |
| Terraform state | S3 | One bucket, `<project_name>-tfstate-<account id>`. One state file per environment (`<project_name>/<environment>/terraform.tfstate`), encrypted, locked with an S3 lock file (`use_lockfile`) |

---

## Prerequisites

Install these before running anything:

```bash
# Terraform >= 1.11
brew install terraform          # macOS
# or: https://developer.hashicorp.com/terraform/install

# AWS CLI v2
brew install awscli             # macOS
# or: https://aws.amazon.com/cli/

# Verify
terraform version               # should show >= 1.11.x
aws --version                   # should show aws-cli/2.x
```

Your AWS credentials must have permission to create IAM roles, VPCs, RDS
instances, ElastiCache clusters, S3 buckets, CloudFront distributions, and
Elastic Beanstalk environments. The simplest approach for a fresh account is
to use an IAM user or role with `AdministratorAccess` during initial setup,
then tighten permissions afterward.

---

## First-time deployment

### Step 1 — Configure AWS credentials

```bash
aws configure
# AWS Access Key ID: <your key>
# AWS Secret Access Key: <your secret>
# Default region name: us-east-1
# Default output format: json
```

Verify it works:
```bash
aws sts get-caller-identity
# Should print your account ID and ARN — no error
```

### Step 2 — Fill in your secrets

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
```

Open `terraform.tfvars` and fill in every value:

| Variable | Where to get it |
|---|---|
| `openai_api_key` | platform.openai.com → API keys (or leave empty and use Anthropic) |
| `anthropic_api_key` | console.anthropic.com → API keys (or leave empty and use OpenAI) |
| `db_password` | Make up a strong password (letters + numbers, min 8 chars) |
| `admin_panel_password` | Test stack only: make up a password — you log into `/api/admin/` with it as user `admin`. Production creates no account at boot (its database has the real admins) |
| `project_name` | A short prefix for every AWS resource (must match `TF_PROJECT_PREFIX`) |
| `environment` | `"test"` or `"production"` |

`django_secret_key` is not in that list on purpose. Leave it empty and Terraform
generates one and keeps it in state, so it stays stable across re-deploys and
active sessions survive. Only set it yourself if you need to pin a specific
value — then it must be at least 50 characters.

### Step 3 — Deploy everything

```bash
bash setup.sh
```

This takes approximately 15–20 minutes. RDS and ElastiCache take the longest.
The script will pause after `terraform plan` and ask you to confirm before
making any changes.

What the script does:
1. Checks that `terraform`, `aws`, and credentials are ready
2. Runs the bootstrap step (creates the S3 state bucket)
3. Runs `terraform init` pointing at that state bucket
4. Shows you the full plan — review it before confirming
5. Applies the plan
6. Prints the 6 deploy secrets of the environment it built

### Step 4 — Configure GitHub Actions secrets

For a production stack, `setup.sh` prints the secrets `production.yml` reads
(`AWS_PROD_ACCOUNT_ID`, `AWS_PROD_ROLE_NAME`, `AWS_S3_PROD_BUCKET_NAME_FRONTEND`,
...). A test stack prints none: it has no app deploy workflow (deploy to it by
hand, see "Manual deployment").

Go to `https://github.com/<org>/<repo>/settings/secrets/actions` and add each
value as a repository secret using the exact name shown. Never copy a value
into the plain `AWS_*` names `staging.yml` reads: that workflow deploys the
hand-built dev site and empties the bucket named in
`AWS_S3_BUCKET_NAME_FRONTEND` before each deploy.

You can re-print these values at any time:
```bash
cd terraform
terraform output
```
The output names (`github_secret_AWS_...`) are not the secret names for
production; use the `AWS_PROD_*` names above.

### Step 5 — First application deployment

For production, push to `main` (triggers `.github/workflows/production.yml`).
For the test stack, deploy by hand ("Manual deployment" below).

The workflow builds the React app, syncs it to S3, invalidates CloudFront, and
deploys the Django backend to Elastic Beanstalk. The first deploy takes about
5 minutes.

When it completes, visit the URLs from terraform output:
```bash
terraform output frontend_url    # https://xxxx.cloudfront.net
terraform output backend_api_url # https://<prefix>-origin.wwbp.org (CloudFront only)
```

The load balancer admits only CloudFront, so check the site through it:
```bash
scripts/smoke.sh $(terraform output -raw frontend_url)   # from the repository root
```

---

## Running Terraform from GitHub Actions

| Where | What | AWS access |
|---|---|---|
| Your laptop | Local containers, the full test suite | None |
| Pull requests | Offline checks only (`ci.yml`, job **Terraform test**): `terraform fmt -check`, `terraform validate` (`init -backend=false`), the mocked `terraform test`, `tests/check_workflows.py` | None: no OIDC, no role, no GitHub Environment, no secrets |
| Test stack | Applied on demand for a rehearsal or a risky change, tested, then destroyed | The shared account, through `<PREFIX>-terraform-apply-test` only |
| Production | Plan, then apply, by hand from `main`, behind the `production` GitHub Environment | The shared account, through `<PREFIX>-terraform-apply-production` only |

`.github/workflows/deploy-infrastructure.yml` has one job, started by hand
(**Actions → Deploy Infrastructure → Run workflow**). Pick `environment`
(`test` or `production`) and `action`: `plan` (the default) prints the plan
and stops; `apply` prints the plan, then applies exactly that plan. A plan
that deletes or replaces anything stops unless `allow_destroy` is ticked. A
second run on the same environment waits for the first. A failed apply
leaves everything in place; the workflow never destroys.

`.github/workflows/destroy-infrastructure.yml` removes a stack:

- **test**: type `test` and approve the run. Nothing is backed up: the test
  stack's data bucket and database are deleted for good. It holds no study
  data. The account is shared with production, the live deployment and other
  lab projects: the run signs in as the test role, which is denied all of
  them, and empties only `<PREFIX>-test-*` buckets. Check the plan's
  resource names in the log anyway. A rehearsal's restored database copy is
  not in the test state; delete it by hand (see the data migration
  runbook).
- **production**: type `production`, `destroy production` and
  `destroy data`, tick `allow_production`, run from `main`, approve. The data
  bucket is copied to the state bucket and verified first, and the database
  leaves a final snapshot.

### Settings

- **Non-secret settings** are in `environments/production.tfvars` and
  `environments/test.tfvars` (instance sizes, `domain_name`, data safety).
  Production keeps `deletion_protection = true`, `backup_retention_period = 7`
  and `force_destroy_buckets = false`; `variables.tf` refuses relaxing them
  anywhere but test. Change them in a pull request.
- **Secrets** are GitHub secrets. Their names are listed at the top of the
  workflow file.

### One account, two roles

Both stacks live in **one AWS account** (`TF_AWS_ACCOUNT_ID`), the one that
also holds the hand-built live deployment (EB application `humanlikebot`,
its `awseb-e-*` RDS instances, the `bot.wwbp.org` / `dev.bot.wwbp.org` /
`bot-avatar.wwbp.org` / `dev.bot-avatar.wwbp.org` buckets and their two
CloudFront distributions) and other lab projects (e.g. MeetLab). Each run
assumes the role of its environment, `<PREFIX>-terraform-apply-production`
or `<PREFIX>-terraform-apply-test`, and refuses credentials from any other
account. One state bucket, `<PREFIX>-tfstate-<account id>`, holds one state
file per environment, `<PREFIX>/<environment>/terraform.tfstate`.

The two roles have the same shape (the test policies are production's with
`test` in place of `production`, so a rehearsal also tests them), and are
isolated by policy:

- **positive scoping**: every Allow names `<PREFIX>-<env>-*` (RDS,
  ElastiCache, Elastic Beanstalk, logs, S3, IAM roles) or requires the tags
  `Environment=<env>` and `Project=<PREFIX>` (EC2, load balancing, Auto
  Scaling, CloudFormation, CloudFront, ACM). That is what keeps other
  projects' resources out; there is no Deny per project;
- the **other environment**, explicitly denied, in the apply policies and
  in the boundaries (by name and tag): the test role everything
  named `<PREFIX>-production-*` or tagged `Environment=production`, and
  production's state key; the production role everything named
  `<PREFIX>-test-*` or tagged `Environment=test`, and the test state key.
  Both are also denied the retired `<PREFIX>-staging-*` names and
  `Environment=staging`;
- the **live deployment**, explicitly denied to both, by name and by the
  tag `elasticbeanstalk:environment-name` = `humanlikebot-env` /
  `humanlike-bot-development-env` (the EB-made instances, security groups,
  load balancers, Auto Scaling groups and `awseb-e-*` CloudFormation stacks);
- **shared account-level resources**: the GitHub OIDC provider is
  read-only to both; in the account's `elasticbeanstalk-<region>-<account>`
  storage bucket each role writes only its own `<PREFIX>-<env>/` keys;
- **the shared state bucket**: one bucket, so production's existing state
  stays where it is. The test role reads and writes only `<PREFIX>/test/`,
  may list no other prefix, may not create, delete or reconfigure the
  bucket (production's first apply, or an administrator, creates it), and
  is denied `<PREFIX>/production/`, where production's state and the
  Destroy workflow's backups live;
- **account-wide reads** are metadata only, from an allowlist that
  `tests/check_workflows.py` enforces: neither role (nor any role a stack
  creates) may read Elastic Beanstalk configuration settings (the
  environment variables, which hold database passwords and API keys),
  CloudFormation stacks (an EB stack's `AWSEBBeanstalkMetadata` holds the
  same settings) or CloudFront functions of anything but its own
  environment, and the apply roles may read instance user data and launch
  template data only on their own environment's resources. Elastic
  Beanstalk names are matched as `application/<PREFIX>-<env>` (a bare
  `*/<PREFIX>-<env>` would also match a template or environment of that name
  in another project's application), and configuration templates, which
  copy another environment's settings, are denied. So neither role can read
  the live deployment's or another project's secrets. The production
  cutover needs none of them: `write_deploy_secrets` reads Terraform's own
  outputs, and the data migration runs with administrator credentials;
- **EC2 inputs**: instances start only from Amazon-owned AMIs and
  snapshots, the apply roles place them only in subnets and security groups
  tagged with their own environment, and no role creates a volume by itself
  (no volume from another project's snapshot). CloudFront origin access
  controls cannot be tagged, so neither role may update one (any
  distribution in the account could depend on it); creating and deleting
  stay allowed, and deleting fails while one is in use;
- **DNS and WAF**: the zone (`wwbp.org`) is shared with the live
  deployment, SES (DKIM, MAIL FROM, DMARC) and other projects. Each apply
  role may change only A, AAAA and CNAME records named
  `<PREFIX>-<env>-origin.<zone>`, production also `<SITE_DOMAIN>`, and their
  ACM validation records (`_` + 32 characters + `.<name>`, which leaves
  `_dmarc` and `_domainkey` names out); it can read the rest. Web ACLs:
  only `<PREFIX>-<env>-*`, never the live distributions' `CreatedByCloudFront-*`.
  The roles' existing policies need this added (re-create them from
  `bootstrap/iam/` with the `sed` below) before the first apply of this
  change; the boundaries are unchanged;
- **email**: the test role and every role the test stack creates (its
  boundary, `<PREFIX>-terraform-boundary-test`) are denied `ses:Send*`, so
  the test stack cannot send real email through the account's SES
  identities. Leave `email_backend` unset in `environments/test.tfvars`:
  its email goes to the application log;
- each role trusts one GitHub Environment: production only on `main`, test
  on `main` or a `test/*` branch (push a risky infrastructure change to
  `test/<name>` to try it before it merges).

Residual risks, accepted for now:

- **`iam:CreateRole`**: each apply role may create `<PREFIX>-<env>-*` roles,
  always under its boundary, but IAM cannot limit the trust policy of a new
  role. A malicious approved run could leave a role that an outside account
  can assume, with the boundary's permissions (its own environment's
  resources, Bedrock spend, metadata reads). Turn on **IAM Access Analyzer**
  (external access, account zone of trust) in the account and act on its
  findings. Removing `iam:CreateRole` means an administrator pre-creates the
  stack's three roles per environment outside Terraform; that restructures
  `iam.tf` and is a separate change.
- The boundaries (at the 6144-character limit) do not restrict which subnets
  and security groups the stack's own roles launch instances into; the
  apply roles do. Production/test crossing is still refused by the tag Deny.
- RDS actions that name a source (`CopyDBSnapshot`,
  `RestoreDBInstanceToPointInTime`, `CreateDBInstanceReadReplica`) are
  authorised on the source ARN too: confirm with
  `aws iam simulate-principal-policy` at the first apply that neither role
  can use another project's database or snapshot as a source.

Shared spend: Bedrock (`bedrock:InvokeModel*` is allowed to both stacks'
instance roles) bills the one account, and nothing separates the test
stack's model use from production's in the bill except the cost allocation
tags. OpenAI and Anthropic usage is separate only if the test Environment
has its own keys (below).

The account is not a throwaway: during a data-migration rehearsal it holds
a restored copy of a production snapshot, with real participant data, and it
always holds the live deployment.
**Approving a test run trusts that branch's code with the shared AWS account**,
within the test role's limits. Read the branch before you approve.

### One-time setup (the owner)

The policies are committed in `bootstrap/iam/` and checked by
`tests/check_workflows.py`. Replace `<ACCOUNT_ID>` (`TF_AWS_ACCOUNT_ID`), `<PREFIX>` (the value of `TF_PROJECT_PREFIX`),
`<SES_FROM_DOMAIN>` and `<SITE_DOMAIN>` (production's public name, `bot.wwbp.org`) before using them, for example:

```bash
for f in bootstrap/iam/*.json; do
  sed -e "s/<ACCOUNT_ID>/$ACCOUNT_ID/g" -e "s/<PREFIX>/$PREFIX/g" \
      -e "s/<SES_FROM_DOMAIN>/$SES_FROM_DOMAIN/g" -e "s/<SITE_DOMAIN>/$SITE_DOMAIN/g" "$f" > "/tmp/$(basename "$f")"
done
```

In the one account, as an administrator, step 1 once and steps 2 and 3
twice (with `<env>` = `production`, then `<env>` = `test`):

1. **GitHub OIDC provider** `token.actions.githubusercontent.com` (audience
   `sts.amazonaws.com`). Check with `aws iam list-open-id-connect-providers`;
   if missing, `aws iam create-open-id-connect-provider --url https://token.actions.githubusercontent.com --client-id-list sts.amazonaws.com`.
   No Terraform stack owns it: the workflows always pass
   `create_github_oidc_provider = false`. If an existing state already owns
   the provider (an older stack built with `create_github_oidc_provider =
   true`), its next plan shows the provider as deleted and the deletion
   check stops the run: move it out of that state with
   `terraform state rm aws_iam_openid_connect_provider.github_actions[0]`
   (by hand, with admin credentials) first.
2. **Permissions boundary** `<PREFIX>-terraform-boundary-<env>`, a customer
   managed policy from `bootstrap/iam/boundary-<env>.json`. It caps every
   role the stack creates; Terraform always attaches it, so create it before
   the first apply. **Roles that already exist** (a stack built before the
   boundary) must get it once, before the apply policy is granted:
   `aws iam put-role-permissions-boundary --role-name <role> --permissions-boundary arn:aws:iam::<ACCOUNT_ID>:policy/<PREFIX>-terraform-boundary-<env>`
   for each `<PREFIX>-<env>-*` role. The apply role cannot change a role's
   trust policy; that is an administrator's job.
3. **Apply role** `<PREFIX>-terraform-apply-<env>`, exactly this name (the
   workflow builds the ARN from it). Trust: `bootstrap/iam/apply-production-trust.json`
   (environment `production`, ref `refs/heads/main`) or
   `bootstrap/iam/apply-test-trust.json` (environment `test`, refs
   `refs/heads/main` and `refs/heads/test/*`).
   Permissions: three customer managed policies (one would exceed IAM's
   6144-character limit), `bootstrap/iam/apply-<env>-permissions.json`,
   `-abac.json` and `-guardrails.json`. They scope RDS, ElastiCache, Elastic
   Beanstalk, logs and S3 to `<PREFIX>-<env>` names, and EC2, load
   balancing, Auto Scaling, CloudFormation, CloudFront and ACM to the
   `Environment=<env>` / `Project=<PREFIX>` tags, and deny the other
   environment. If the first test apply fails with AccessDenied, fix the
   test policy narrowly (a name or tag scope, never a broad Allow: the
   account is shared with production) and carry the same fix to production
   in a pull request. **Re-apply all three policies of both roles** from
   this commit: the production guardrails now deny the test stack, and the
   test guardrails and boundary deny `ses:Send*`.

In GitHub (**Settings**):

4. **Environments**:
   - `production`: **Required reviewers**: the owner and one senior
     colleague; **Prevent self-review**: off (the owner approves their own
     runs); **Deployment branches**: `main` only.
   - `test`: **Required reviewers**: the owner; **Deployment branches**: `main` and `test/*`
     (the same refs `apply-test-trust.json` accepts).
   Without required reviewers, anyone who can run workflows can apply.
5. **Environment secrets**, in `production` and in `test` (different values):
   `DB_PASSWORD`, `ADMIN_PANEL_PASSWORD` (test only: production creates no
   admin at boot), and `OPENAI_API_KEY` and/or
   `ANTHROPIC_API_KEY`. Optional: `SURVEY_INGEST_TOKEN`. The test stack uses
   its own LLM API keys, scoped to it (a separate project or key with a low
   spending limit), never production's. In `production` only: `GH_PAT`, if
   you use `write_deploy_secrets` (it can write repository secrets, so keep
   it out of the repository secrets, where any workflow run could read it).
6. **Repository secrets**: `TF_PROJECT_PREFIX`, `TF_AWS_ACCOUNT_ID` (the
   one account, 12 digits).
7. **Delete what is left of the old setup**: the Environments
   `plan-staging` and `plan-production`, the secrets `TF_PLAN_ROLE_ARN`,
   `TF_APPLY_ROLE_ARN`, `TF_PROD_ACCOUNT_ID` and `TF_TEST_ACCOUNT_ID`
   (wherever they were set), a repository-level `GH_PAT`, and, in AWS, the role
   `<PREFIX>-terraform-plan-staging` and any `<PREFIX>-terraform-apply-staging`
   role or `<PREFIX>-terraform-boundary-staging` policy. Keep the plain
   `AWS_*` repository secrets: `staging.yml` still deploys the hand-built dev
   site with them.

Then run the workflow from `main` or a `test/*` branch: environment `test`, action `apply`. The
test role cannot create the shared state bucket: if no production apply has
created it yet, create it first from `bootstrap/` (as an administrator,
`terraform -chdir=bootstrap apply -var project_name=$PREFIX`).

### Secrets in the state file

The Terraform state still holds the database password, the admin password,
the API keys and the Django secret key. Anyone who can run a plan for an
environment can read its secrets; that is why every run needs an approval in
that environment's GitHub Environment, and pull requests get no AWS access.
Moving the secrets out of the state into Secrets Manager is a separate,
later change.

## Offline checks

`terraform test` checks the plan against a mocked AWS provider, so it needs no
account or credentials. CI runs it on every pull request:

```bash
terraform fmt -check -recursive
terraform init -backend=false && terraform validate && terraform test
```

`tests/check_workflows.py` reads the workflows, the IAM policies and the
settings files as text and fails if a known hazard comes back (destroy on
failure, a hardcoded environment, a shared state key, a missing state lock,
AWS access on a pull request, a role reaching the other environment, a
relaxed production setting). CI runs it in the same job:

```bash
python3 infra/terraform/tests/check_workflows.py   # from the repository root
```

## The test stack by hand

Prefer the workflow. By hand, Terraform runs with your own credentials, not
the test role (it trusts only GitHub), so none of its Denies apply, in the
account that also holds production and the live deployment. If you must,
run `setup.sh` with `environment = "test"` in `terraform.tfvars`, and read
the plan: every resource must be named `<PREFIX>-test-*`.

```bash
bash setup.sh
terraform destroy -var-file=terraform.tfvars -var-file=environments/test.tfvars
```

`environments/test.tfvars` turns deletion protection off and lets the
buckets be force-destroyed, so the destroy needs no extra steps. Use the
Destroy Infrastructure workflow for a stack the workflow built.

---

## Email (staff invites and password resets)

The application sends invites and password resets through Amazon SES, using
the instance role. Three variables control it:

| Variable | Sets | Value |
|---|---|---|
| `site_url` | `SITE_URL` | The public URL, e.g. `https://chatbot.mylab.org`. Empty: `https://<domain_name>` if a custom domain is set, otherwise unset (invites then fail with a warning in the admin). Production: `https://bot.wwbp.org` in `environments/production.tfvars` |
| `email_backend` | `EMAIL_BACKEND` | Empty for the default: `chatbot.email_backend.SESBackend` on production; on the test stack `django.core.mail.backends.console.EmailBackend` (emails go to the log), the only backend the test stack accepts, so a rehearsal never writes to real participants |
| `default_from_email` | `DEFAULT_FROM_EMAIL` | The sender. Empty for the default: `ChatbotLab <no-reply@bot.wwbp.org>` on production, none on the test stack |

With the SES backend, Terraform also gives the instance role one permission:
`ses:SendEmail` / `ses:SendRawEmail` on the sender's address and domain
identities only (on production `identity/no-reply@bot.wwbp.org` and
`identity/bot.wwbp.org`), only when the From address equals the sender.
Otherwise the role gets no SES permission.

**Terraform does not verify the sender or leave the SES sandbox. Do both by
hand, once per AWS account and region, before the first production apply.**
`bot.wwbp.org` is already verified (Easy DKIM, MAIL FROM `mail.bot.wwbp.org`)
and the production account is out of the sandbox; a new account needs:

1. In the SES console (same region as `aws_region`), open **Identities →
   Create identity**. Choose the domain of the sender (e.g. `mylab.org`), or
   the single address.
2. For a domain, add the DKIM CNAME records SES shows to the domain's DNS and
   wait for the status **Verified**. For an address, click the link in the
   confirmation email.
3. A new account is in the SES sandbox and can send only to verified
   addresses. In **Account dashboard → Request production access**, ask AWS to
   lift it. Until then, invites to other people are rejected.
4. Send a test: in the admin, invite a user, or use **Send set-password
   email** on an existing one.

These steps are manual because each needs a person: a DNS change or a click in
a mailbox, and a support request that AWS reviews.

---

## Ongoing development

### Deploying application changes

No Terraform involvement. Push to the branch:

```bash
git push origin staging     # → the hand-built dev site (dev.bot.wwbp.org)
git push origin main        # → production
```

GitHub Actions builds, syncs, and deploys automatically.

### Changing infrastructure

Edit the relevant `.tf` file, then preview and apply:

```bash
cd terraform
terraform plan -var-file=terraform.tfvars    # review what will change
terraform apply -var-file=terraform.tfvars   # apply it
```

Common changes:

**Scale up the application server:**
```hcl
# terraform.tfvars
eb_instance_type = "t3.medium"   # was t3.small
```

**Scale up the database:**
```hcl
# terraform.tfvars
db_instance_class = "db.t3.small"   # was db.t3.micro
```

**Rotate an API key:**
```hcl
# terraform.tfvars
openai_api_key = "sk-proj-new-key-here"
```
Then `terraform apply` — EB picks up the new env var on the next instance refresh.

**Add a custom domain:**
1. Create an ACM certificate for your domain in us-east-1 (AWS console)
2. Edit `cloudfront.tf`: add `aliases = ["your-domain.com"]` to the distribution
   and replace `cloudfront_default_certificate = true` with:
   ```hcl
   viewer_certificate {
     acm_certificate_arn      = "arn:aws:acm:us-east-1:..."
     ssl_support_method       = "sni-only"
     minimum_protocol_version = "TLSv1.2_2021"
   }
   ```
3. `terraform apply`
4. Point your domain's CNAME at the CloudFront domain name from `terraform output frontend_url`

### Viewing Terraform state

```bash
terraform state list                              # all resources
terraform state show aws_db_instance.main         # one resource in detail
terraform output                                  # all outputs
terraform output -raw redis_url                   # one sensitive output
```

### If something goes wrong mid-apply

Terraform stores what it completed in the state file. Re-running apply is safe —
it only creates or updates what is missing or changed.

```bash
terraform apply -var-file=terraform.tfvars   # picks up from where it left off
```

---

## Manual deployment

After `setup.sh` completes and the infrastructure is up, deploy the app manually:

### Backend (Django on Elastic Beanstalk)

```bash
cd api
pip install awsebcli
eb init <project-prefix> --platform docker --region us-east-1
eb use <project-prefix>-test-env
eb deploy
```

On first deploy, set the FRONTEND_URL env var (CloudFront domain from `terraform output frontend_url`):

```bash
CF_DOMAIN=$(cd terraform && terraform output -raw frontend_url)
aws elasticbeanstalk update-environment \
  --environment-name <project-prefix>-test-env \
  --option-settings Namespace=aws:elasticbeanstalk:application:environment,OptionName=FRONTEND_URL,Value="https://${CF_DOMAIN}"
```

To create a Django admin superuser on first deploy, set these env vars before `eb deploy`:

```bash
aws elasticbeanstalk update-environment \
  --environment-name <project-prefix>-test-env \
  --option-settings \
    Namespace=aws:elasticbeanstalk:application:environment,OptionName=DJANGO_SUPERUSER_PASSWORD,Value="YourPassword" \
    Namespace=aws:elasticbeanstalk:application:environment,OptionName=DJANGO_SUPERUSER_USERNAME,Value="admin" \
    Namespace=aws:elasticbeanstalk:application:environment,OptionName=DJANGO_SUPERUSER_EMAIL,Value="admin@example.com"
# Wait for env to be Ready, then deploy
eb deploy
# Remove the password var after the user is created
aws elasticbeanstalk update-environment \
  --environment-name <project-prefix>-test-env \
  --options-to-remove Namespace=aws:elasticbeanstalk:application:environment,OptionName=DJANGO_SUPERUSER_PASSWORD
```

### Frontend (React on S3 + CloudFront)

```bash
cd react-frontend    # or wherever the React app lives
npm ci
npm run build
# Sync to S3
aws s3 sync build/ s3://$(cd ../terraform && terraform output -raw frontend_bucket_name) --delete
# Invalidate CloudFront cache
aws cloudfront create-invalidation \
  --distribution-id $(cd ../terraform && terraform output -raw cloudfront_distribution_id) \
  --paths "/*"
```

### Access

| URL | What |
|---|---|
| `https://<cloudfront-domain>/` | React frontend |
| `https://<cloudfront-domain>/api/` | Django REST API |
| `https://<cloudfront-domain>/api/admin/` | Django admin (login with superuser credentials above) |
| `https://<cloudfront-domain>/api/health/ready/` | Readiness check (database + cache) |

---

## Pending

### Performance testing

Not yet done. Suggested baseline before any load test:

1. **Health endpoint** — `curl https://<cf-domain>/api/health/ready/` should return 200 in < 200 ms
2. **API under load** — use `ab` or `k6` against `/api/` endpoints; watch EB CPU and RDS connections
3. **WebSocket** — verify the `/ws/` path stays open under concurrent connections
4. **CloudFront cache hit rate** — check CloudFront metrics in the AWS console after serving real traffic

---

## File reference

| File | What it does |
|---|---|
| `bootstrap/main.tf` | Creates the S3 bucket for Terraform state. Run once per AWS account. |
| `main.tf` | AWS provider (v6.4) and empty S3 backend declaration |
| `variables.tf` | All input variables with descriptions |
| `locals.tf` | Shared naming convention and common tags |
| `vpc.tf` | VPC, subnets, internet gateway, route tables, subnet groups |
| `security_groups.tf` | Firewall rules — ALB → instances → database/cache chain |
| `iam.tf` | EB instance profile, EB service role, GitHub OIDC role |
| `rds.tf` | MySQL 8.4 database |
| `elasticache.tf` | Redis Serverless cache |
| `s3.tf` | Frontend assets bucket and application data bucket |
| `cloudfront.tf` | CloudFront distribution with OAC and SPA routing |
| `elasticbeanstalk.tf` | EB application and environment — all env vars wired here |
| `outputs.tf` | Printed after apply: URLs, GitHub secrets, debug values |
| `terraform.tfvars.example` | Copy to `terraform.tfvars` and fill in secrets |
| `setup.sh` | One-command deploy: bootstrap → init → plan → apply → print secrets |
