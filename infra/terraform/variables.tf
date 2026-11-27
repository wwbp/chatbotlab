# -----------------------------------------------------------------------------
# variables.tf — every input this configuration accepts
#
# Sensitive variables (marked sensitive = true) are never printed in plan
# output or stored in plaintext in state. They still live in state as
# encrypted values — which is why the state bucket uses AES-256 encryption.
#
# Fill in actual values in terraform.tfvars (copy from terraform.tfvars.example).
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------
# Core deployment settings
# -----------------------------------------------------------------------

variable "aws_region" {
  description = "The AWS region where I deploy all infrastructure. I default to us-east-1 because that is where the existing ElastiCache cluster and EB environment live."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "A short identifier prepended to every resource name I create. Changing this after the first deploy forces recreation of all resources, so set it once and leave it. DO NOT change this to match the chatbotlab repository name: it is the prefix on live S3 buckets, the RDS instance, the EB application and the Terraform state bucket, and renaming it would destroy the production database. The repo name lives in var.github_repo instead. Deliberately has no default — the CI workflows pass it via TF_VAR_project_name from the TF_PROJECT_PREFIX secret, and a wrong or empty value here would rename every resource, so I fail loudly instead of guessing."
  type        = string

  validation {
    condition     = length(var.project_name) > 0
    error_message = "project_name must not be empty — set TF_PROJECT_PREFIX (CI) or project_name in terraform.tfvars (local)."
  }
}

variable "environment" {
  description = "Which deployment environment I am provisioning: production (study data, in the production AWS account) or test (the on-demand stack, applied and destroyed when needed; it shares the production account, with every resource named <project>-test)."
  type        = string
  validation {
    condition     = contains(["test", "production"], var.environment)
    error_message = "I only accept 'test' or 'production' as valid environments."
  }
}

# -----------------------------------------------------------------------
# Data safety. The defaults are production's; only the test stack may relax
# them (environments/test.tfvars). Production refuses any relaxation.
# -----------------------------------------------------------------------

variable "deletion_protection" {
  description = "RDS deletion protection. Must stay true for production; the test stack turns it off so it can be destroyed on demand."
  type        = bool
  default     = true
  validation {
    condition     = var.deletion_protection || var.environment == "test"
    error_message = "Only the test stack may turn deletion_protection off."
  }
}

variable "backup_retention_period" {
  description = "Days of RDS automated backups (point-in-time restore window). At least 7 for production; the test stack may keep fewer."
  type        = number
  default     = 7
  validation {
    condition     = var.backup_retention_period >= 7 || var.environment == "test"
    error_message = "Production must keep at least 7 days of backups; only the test stack may keep fewer."
  }
}

variable "force_destroy_buckets" {
  description = "Let terraform destroy delete the S3 buckets with their objects. Only for the test stack: production's data bucket holds participant uploads."
  type        = bool
  default     = false
  validation {
    condition     = !var.force_destroy_buckets || var.environment == "test"
    error_message = "Only the test stack may force-destroy its buckets."
  }
}

# -----------------------------------------------------------------------
# Secrets — these are passed in at deploy time and never hardcoded
# -----------------------------------------------------------------------

variable "openai_api_key" {
  description = "The OpenAI API key I inject into the Django application for chat and content moderation. Generate one at platform.openai.com. Leave empty if using Anthropic only."
  type        = string
  sensitive   = true
  default     = ""
}

variable "anthropic_api_key" {
  description = "The Anthropic API key I inject into the Django application for Claude-based chat models. Generate one at console.anthropic.com. Leave empty if using OpenAI only."
  type        = string
  sensitive   = true
  default     = ""
}

variable "survey_ingest_token" {
  description = "Shared secret the survey tool (e.g. a Qualtrics Web Service element) sends as the X-Survey-Token header when POSTing participant answers to /api/survey_response/. Leave empty to disable the endpoint entirely — it fails closed and rejects every request when unset, so only set this for studies that feed survey answers to the bot. Generate one with: openssl rand -hex 32"
  type        = string
  sensitive   = true
  default     = ""
}

variable "db_password" {
  description = "The master password for the MySQL database I create on RDS. Use letters and numbers only — special characters can break the MySQL connection string."
  type        = string
  sensitive   = true
}

variable "django_secret_key" {
  description = "The cryptographic signing key Django uses for sessions and CSRF tokens. Leave empty to auto-generate a stable key (recommended — random_password.django_secret_key is kept in state and reused on every deploy, so sessions survive re-deploys). If you set it yourself, use at least 50 characters."
  type        = string
  sensitive   = true
  default     = ""
  validation {
    condition     = var.django_secret_key == "" || length(var.django_secret_key) >= 50
    error_message = "A caller-supplied Django secret key must be at least 50 characters. Leave it empty to auto-generate one instead."
  }
}

variable "admin_panel_password" {
  description = "Test stack only: the password of the 'admin' superuser that api/entrypoint.sh creates in the empty database (DJANGO_SUPERUSER_PASSWORD). Production ignores it: its database has the real admins, and it creates no account at boot."
  type        = string
  sensitive   = true
  default     = ""

  validation {
    condition     = var.environment != "test" || var.admin_panel_password != ""
    error_message = "The test stack needs admin_panel_password: its database starts empty, and the admin account is the only way into /api/admin/."
  }
}

# -----------------------------------------------------------------------
# Email — staff invites and password resets
# -----------------------------------------------------------------------

variable "site_url" {
  description = "The public URL of the site, e.g. https://chatbot.mylab.org, with no trailing slash. The application builds invite and password-reset links from it (SITE_URL). Leave empty to use https://<domain_name> when a custom domain is set. With neither set, invites fail with a warning in the admin."
  type        = string
  default     = ""
}

variable "email_backend" {
  description = "The Django email backend (EMAIL_BACKEND). Leave empty for the default: chatbot.email_backend.SESBackend (Amazon SES) on production, django.core.mail.backends.console.EmailBackend (the log) on the test stack, which may use nothing else."
  type        = string
  default     = ""

  validation {
    condition     = var.environment != "test" || contains(["", "django.core.mail.backends.console.EmailBackend"], var.email_backend)
    error_message = "The test stack prints emails to its log, so a rehearsal never writes to real participants."
  }
}

variable "default_from_email" {
  description = "The sender of invites and password resets (DEFAULT_FROM_EMAIL), as an address or as 'Name <address>', e.g. \"ChatbotLab <no-reply@mylab.org>\". With the SES backend, the instance role may send from exactly that address. Leave empty for the default: ChatbotLab <no-reply@bot.wwbp.org> on production, the application's own default on the test stack."
  type        = string
  default     = ""

  validation {
    condition     = var.default_from_email == "" || can(regex("^[^@\\s<>]+@[^@\\s<>]+$", trimspace(try(regex("<([^<>]*)>\\s*$", var.default_from_email)[0], var.default_from_email))))
    error_message = "default_from_email must be an address (no-reply@mylab.org) or a name and an address (ChatbotLab <no-reply@mylab.org>)."
  }
}

# -----------------------------------------------------------------------
# GitHub — used to set up passwordless CI/CD via OIDC
# -----------------------------------------------------------------------

variable "github_org" {
  description = "The GitHub organization (or username) that owns this repository. I use this to restrict which GitHub Actions workflows can assume the deployment IAM role."
  type        = string
  default     = "wwbp"
}

variable "github_repo" {
  description = "The GitHub repository name (without the org prefix). I use this alongside github_org to scope OIDC trust to exactly this repo. Note this is the *repository* name, which is deliberately not the same as project_name — the repo was renamed to chatbotlab, while project_name stays humanlike-chatbot because it names live AWS resources."
  type        = string
  default     = "chatbotlab"
}

variable "github_org_id" {
  description = "Numeric GitHub organization ID, used for the immutable OIDC subject claim (repo:<org>@<org_id>/...). GitHub issues subjects carrying these IDs so a trust policy cannot be satisfied by re-creating a deleted org or repo under the same name; a policy matching only the name-based format stops working once they are issued. Leave empty to trust the name-based format alone. Find it with: gh api orgs/<org> --jq .id"
  type        = string
  default     = "42522115" # wwbp
}

variable "github_repo_id" {
  description = "Numeric GitHub repository ID, the counterpart to github_org_id for the immutable OIDC subject claim. Leave empty to trust the name-based format alone. Find it with: gh api repos/<org>/<repo> --jq .id"
  type        = string
  default     = "882137210" # wwbp/chatbotlab
}

# -----------------------------------------------------------------------
# Instance sizing
# -----------------------------------------------------------------------

variable "eb_instance_type" {
  description = "The EC2 instance type I use for the Elastic Beanstalk application servers. t3.small is a good starting point — upgrade to t3.medium if the app feels slow under load."
  type        = string
  default     = "t3.small"
}

variable "eb_max_instances" {
  description = "The maximum number of EC2 instances the auto-scaling group can launch. 1 suits most studies; increase it if you expect hundreds of simultaneous conversations."
  type        = number
  default     = 1
}

variable "db_instance_class" {
  description = "The RDS instance class for the MySQL database. db.t3.micro is free-tier eligible and sufficient for the test stack. Consider db.t3.small or db.t3.medium for production."
  type        = string
  default     = "db.t3.micro"
}

variable "domain_name" {
  description = "Optional custom domain to serve the chatbot from (e.g. chatbot.mylab.org). Leave empty to use the auto-assigned CloudFront URL (https://xxxx.cloudfront.net). If set, I create an SSL certificate and you must add a DNS validation record at your registrar — the deploy workflow prints it to the run summary."
  type        = string
  default     = ""
}

variable "create_github_oidc_provider" {
  description = "Set to false if a GitHub Actions OIDC provider already exists in this AWS account. Each account allows exactly one OIDC provider per URL — creating a second one for token.actions.githubusercontent.com will fail. Check with: aws iam list-open-id-connect-providers"
  type        = bool
  default     = true
}

# -----------------------------------------------------------------------
# Edge: CloudFront's web ACL and DNS
# -----------------------------------------------------------------------

variable "waf_enabled" {
  description = "Put the web ACL in waf.tf (the live production distribution's rules) in front of CloudFront. It costs about $11 a month plus $0.60 per million requests. Must stay true for production; the test stack may turn it off."
  type        = bool
  default     = true
  validation {
    condition     = var.waf_enabled || var.environment == "test"
    error_message = "Only the test stack may turn the web ACL off."
  }
}

variable "dns_zone_name" {
  description = "The public Route 53 hosted zone, already in this account, that holds this stack's DNS records: <prefix>-origin.<zone> (CloudFront's name for the load balancer) and, with domain_name, the certificate validation and alias records. Terraform reads the zone and adds only those names; it never manages the zone or its other records."
  type        = string
  default     = "wwbp.org"
}
