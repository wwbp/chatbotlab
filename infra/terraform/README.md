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
> Staging and production were built **manually**, before this IaC existed, and
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
> To validate the IaC, run it with a distinct `TF_PROJECT_PREFIX` (for example
> `chatbotlab-iac-test`), ideally in a separate AWS account, and tear it down
> afterwards with the Destroy Infrastructure workflow.

---

## What gets created

| Resource | Type | Purpose |
|---|---|---|
| VPC | 10.0.0.0/16 across 2 AZs | Private network — nothing reaches the DB or cache except the app |
| Public subnets | 10.0.1/24, 10.0.2/24 | Load balancer and application instances |
| Private subnets | 10.0.10/24, 10.0.11/24 | RDS and ElastiCache — no internet route |
| Security groups | 4 (ALB → EB → RDS/Cache) | Least-privilege firewall chain |
| RDS MariaDB 10.11 | db.t3.micro (staging) | Primary database, encrypted, automated backups |
| ElastiCache Serverless Redis | TLS-only | Django session cache |
| S3 frontend bucket | Private + CloudFront OAC | Compiled React app |
| S3 data bucket | Private + CORS | Avatars, uploads, voice audio |
| CloudFront distribution | OAC, SPA routing | CDN for the React app |
| Elastic Beanstalk | Docker on AL2023, ALB | Django API server |
| IAM instance role | Least privilege | App reads/writes S3, calls Bedrock — no hardcoded keys |
| IAM GitHub OIDC role | Scoped to this repo | GitHub Actions deploys without storing AWS keys |
| Terraform state | S3 + DynamoDB | Shared, locked, encrypted — created by bootstrap |

---

## Prerequisites

Install these before running anything:

```bash
# Terraform >= 1.9
brew install terraform          # macOS
# or: https://developer.hashicorp.com/terraform/install

# AWS CLI v2
brew install awscli             # macOS
# or: https://aws.amazon.com/cli/

# Verify
terraform version               # should show >= 1.9.x
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
| `admin_panel_password` | Make up a password — you log into `/api/admin/` with it as user `admin` |
| `project_name` | A short prefix for every AWS resource (must match `TF_PROJECT_PREFIX`) |
| `environment` | `"staging"` or `"production"` |

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
2. Runs the bootstrap step (creates the S3 state bucket and DynamoDB lock table)
3. Runs `terraform init` pointing at that state bucket
4. Shows you the full plan — review it before confirming
5. Applies the plan
6. Prints 9 GitHub secrets to configure

### Step 4 — Configure GitHub Actions secrets

After `setup.sh` finishes, it prints output like:

```
AWS_ACCOUNT_ID                                    123456789012
AWS_ROLE_NAME                                     <project-prefix>-staging-github-actions
AWS_S3_BUCKET_NAME_FRONTEND                       <project-prefix>-staging-frontend
...
```

Go to `https://github.com/<org>/<repo>/settings/secrets/actions` and add each
value as a repository secret using the exact name shown.

You can re-print these values at any time:
```bash
cd terraform
terraform output
```

### Step 5 — First application deployment

Push to the branch that triggers your workflow:

```bash
git push origin staging    # triggers .github/workflows/staging.yml
```

The workflow builds the React app, syncs it to S3, invalidates CloudFront, and
deploys the Django backend to Elastic Beanstalk. The first deploy takes about
5 minutes.

When it completes, visit the URLs from terraform output:
```bash
terraform output frontend_url    # https://xxxx.cloudfront.net
terraform output backend_api_url # http://xxxx.elasticbeanstalk.com
```

Test the backend health check:
```bash
curl $(terraform output -raw backend_api_url)/health/
# {"status": "ok"}
```

---

## Testing on a separate AWS account

Use this to validate Terraform changes without touching the existing staging
or production deployment.

### On your machine (Terraform apply)

Configure credentials for the test account in a named profile:

```bash
aws configure --profile test-account
# Enter the test account keys when prompted
```

Run setup with that profile:
```bash
cd terraform
AWS_PROFILE=test-account bash setup.sh
# Enter environment="staging" and a test project_name (e.g. "chatbot-test")
# in terraform.tfvars to keep resource names distinct
```

### In GitHub Actions (CI testing)

A dedicated workflow at `.github/workflows/test-iac.yml` triggers on the
`tf-iac` branch and uses `TEST_` prefixed secrets so it never touches the
existing staging secrets.

After `setup.sh` finishes on the test account, add these GitHub secrets
(prefix every terraform output name with `TEST_`):

| GitHub secret | Value from `terraform output` |
|---|---|
| `TEST_AWS_ACCOUNT_ID` | `github_secret_AWS_ACCOUNT_ID` |
| `TEST_AWS_ROLE_NAME` | `github_secret_AWS_ROLE_NAME` |
| `TEST_AWS_S3_BUCKET_NAME_FRONTEND` | `github_secret_AWS_S3_BUCKET_NAME_FRONTEND` |
| `TEST_AWS_CLOUDFRONT_DISTRIBUTION_ID` | `github_secret_AWS_CLOUDFRONT_DISTRIBUTION_ID` |
| `TEST_AWS_BACKEND_APPLICATION_NAME` | `github_secret_AWS_BACKEND_APPLICATION_NAME` |
| `TEST_AWS_BACKEND_ENVIRONMENT_NAME` | `github_secret_AWS_BACKEND_ENVIRONMENT_NAME` |
| `TEST_REACT_APP_API_URL` | `http://` + `terraform output -raw backend_api_url` + `/api` |

Then push to `tf-iac` to trigger the test workflow.

### Tear down the test environment

```bash
AWS_PROFILE=test-account terraform destroy -var-file=terraform.tfvars
```

On staging the destroy completes without extra steps.
On production, you must first disable deletion protection:
```bash
# Edit rds.tf: set deletion_protection = false, then:
AWS_PROFILE=test-account terraform apply -var-file=terraform.tfvars
AWS_PROFILE=test-account terraform destroy -var-file=terraform.tfvars
```

---

## Ongoing development

### Deploying application changes

No Terraform involvement. Just push to the appropriate branch:

```bash
git push origin staging     # → staging environment
git push origin main        # → production environment
git push origin tf-iac      # → test environment
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

## Manual deployment (tf-iac / test branch)

After `setup.sh` completes and the infrastructure is up, deploy the app manually:

### Backend (Django on Elastic Beanstalk)

```bash
cd api
pip install awsebcli
eb init <project-prefix> --platform docker --region us-east-1
eb use <project-prefix>-staging-env
eb deploy
```

On first deploy, set the FRONTEND_URL env var (CloudFront domain from `terraform output frontend_url`):

```bash
CF_DOMAIN=$(cd terraform && terraform output -raw frontend_url)
aws elasticbeanstalk update-environment \
  --environment-name <project-prefix>-staging-env \
  --option-settings Namespace=aws:elasticbeanstalk:application:environment,OptionName=FRONTEND_URL,Value="https://${CF_DOMAIN}"
```

To create a Django admin superuser on first deploy, set these env vars before `eb deploy`:

```bash
aws elasticbeanstalk update-environment \
  --environment-name <project-prefix>-staging-env \
  --option-settings \
    Namespace=aws:elasticbeanstalk:application:environment,OptionName=DJANGO_SUPERUSER_PASSWORD,Value="YourPassword" \
    Namespace=aws:elasticbeanstalk:application:environment,OptionName=DJANGO_SUPERUSER_USERNAME,Value="admin" \
    Namespace=aws:elasticbeanstalk:application:environment,OptionName=DJANGO_SUPERUSER_EMAIL,Value="admin@example.com"
# Wait for env to be Ready, then deploy
eb deploy
# Remove the password var after the user is created
aws elasticbeanstalk update-environment \
  --environment-name <project-prefix>-staging-env \
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
| `https://<cloudfront-domain>/health/` | Health check |

---

## Pending

### GitHub Actions CI/CD

The IAM OIDC role (`<project-prefix>-staging-github-actions`) and the workflow file
(`.github/workflows/test-iac.yml`) are in place. What's missing is the `TEST_*`
GitHub repository secrets. After `setup.sh` finishes on the test account, add:

| GitHub secret | Value |
|---|---|
| `TEST_AWS_ACCOUNT_ID` | `terraform output -raw github_secret_AWS_ACCOUNT_ID` |
| `TEST_AWS_ROLE_NAME` | `terraform output -raw github_secret_AWS_ROLE_NAME` |
| `TEST_AWS_S3_BUCKET_NAME_FRONTEND` | `terraform output -raw github_secret_AWS_S3_BUCKET_NAME_FRONTEND` |
| `TEST_AWS_CLOUDFRONT_DISTRIBUTION_ID` | `terraform output -raw github_secret_AWS_CLOUDFRONT_DISTRIBUTION_ID` |
| `TEST_AWS_BACKEND_APPLICATION_NAME` | `terraform output -raw github_secret_AWS_BACKEND_APPLICATION_NAME` |
| `TEST_AWS_BACKEND_ENVIRONMENT_NAME` | `terraform output -raw github_secret_AWS_BACKEND_ENVIRONMENT_NAME` |
| `TEST_REACT_APP_API_URL` | `http://` + `terraform output -raw backend_api_url` + `/api` |

Once secrets are set, push to `tf-iac` to trigger the workflow — it will build and
deploy both frontend and backend automatically.

### Performance testing

Not yet done. Suggested baseline before any load test:

1. **Health endpoint** — `curl https://<cf-domain>/health/` should return 200 in < 200 ms
2. **API under load** — use `ab` or `k6` against `/api/` endpoints; watch EB CPU and RDS connections
3. **WebSocket** — verify the `/ws/` path stays open under concurrent connections
4. **CloudFront cache hit rate** — check CloudFront metrics in the AWS console after serving real traffic

---

## File reference

| File | What it does |
|---|---|
| `bootstrap/main.tf` | Creates the S3 bucket and DynamoDB table for Terraform state. Run once. |
| `main.tf` | AWS provider (v6.4) and empty S3 backend declaration |
| `variables.tf` | All input variables with descriptions |
| `locals.tf` | Shared naming convention and common tags |
| `vpc.tf` | VPC, subnets, internet gateway, route tables, subnet groups |
| `security_groups.tf` | Firewall rules — ALB → instances → database/cache chain |
| `iam.tf` | EB instance profile, EB service role, GitHub OIDC role |
| `rds.tf` | MariaDB 10.11 database |
| `elasticache.tf` | Redis Serverless cache |
| `s3.tf` | Frontend assets bucket and application data bucket |
| `cloudfront.tf` | CloudFront distribution with OAC and SPA routing |
| `elasticbeanstalk.tf` | EB application and environment — all env vars wired here |
| `outputs.tf` | Printed after apply: URLs, GitHub secrets, debug values |
| `terraform.tfvars.example` | Copy to `terraform.tfvars` and fill in secrets |
| `setup.sh` | One-command deploy: bootstrap → init → plan → apply → print secrets |
