# Runs offline: `terraform init -backend=false && terraform test`.
# Production's environment variables, compared with what the application reads
# (api/env_vars.txt, kept in step with the code by api/chatbot/tests/
# test_env_vars.py) and with what the hand-built production environment sets
# today (the 2026-11-25 inventory, names only).

mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{}"
    }
  }
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
    }
  }
}

mock_provider "aws" {
  alias = "us_east_1"
}

mock_provider "random" {}

# environments/production.tfvars (check_workflows.py checks the file).
variables {
  project_name         = "chatbotlab"
  environment          = "production"
  site_url             = "https://bot.wwbp.org"
  db_password          = "test-password-not-real"
  admin_panel_password = "test-password-not-real"
}

# Names come from the first word of each line of api/env_vars.txt; a line
# with "unset:" after the name says why Terraform leaves it out. The settings
# set holds secrets, hence nonsensitive() on the names.
run "production_sets_every_name_the_app_reads" {
  command = plan

  assert {
    condition = length(setsubtract(
      regexall("(?m)^[A-Z][A-Z0-9_]*", file("../../api/env_vars.txt")),
      setunion(
        nonsensitive([for s in aws_elastic_beanstalk_environment.chatbot_api.setting : s.name if s.namespace == "aws:elasticbeanstalk:application:environment"]),
        flatten(regexall("(?m)^([A-Z][A-Z0-9_]*)\\s+unset:", file("../../api/env_vars.txt"))),
      ),
    )) == 0
    error_message = "Production must set every name in api/env_vars.txt, or the file must mark it 'unset:' with the reason. Missing: ${join(", ", setsubtract(regexall("(?m)^[A-Z][A-Z0-9_]*", file("../../api/env_vars.txt")), setunion(nonsensitive([for s in aws_elastic_beanstalk_environment.chatbot_api.setting : s.name]), flatten(regexall("(?m)^([A-Z][A-Z0-9_]*)\\s+unset:", file("../../api/env_vars.txt"))))))}"
  }

  # AWS_ACCESS_KEY_ID would make boto3 use a static key instead of the
  # instance role; MOCK_*_P50_MS only matter with MOCK_LLM on.
  assert {
    condition = length(setintersection(
      nonsensitive([for s in aws_elastic_beanstalk_environment.chatbot_api.setting : s.name]),
      flatten(regexall("(?m)^([A-Z][A-Z0-9_]*)\\s+unset:", file("../../api/env_vars.txt"))),
    )) == 0
    error_message = "A name api/env_vars.txt marks 'unset:' must not be set by Terraform."
  }

  # Everything Terraform sets is read by the application: no dead settings.
  assert {
    condition = length(setsubtract(
      nonsensitive([for s in aws_elastic_beanstalk_environment.chatbot_api.setting : s.name if s.namespace == "aws:elasticbeanstalk:application:environment"]),
      regexall("(?m)^[A-Z][A-Z0-9_]*", file("../../api/env_vars.txt")),
    )) == 0
    error_message = "Terraform sets an environment variable the application never reads: ${join(", ", setsubtract(nonsensitive([for s in aws_elastic_beanstalk_environment.chatbot_api.setting : s.name if s.namespace == "aws:elasticbeanstalk:application:environment"]), regexall("(?m)^[A-Z][A-Z0-9_]*", file("../../api/env_vars.txt"))))}"
  }
}

# Every name the live production or staging environment sets is either kept
# (read by the app, so covered above) or dropped on purpose here because no
# code reads it:
#   BASE_URL                 SITE_URL replaced it
#   CORS_ALLOW_CREDENTIALS   settings.py never reads it
#   REACT_APP_API_URL        the front end reads VITE_API_URL at build time
run "every_live_name_is_kept_or_dropped_on_purpose" {
  command = plan

  assert {
    condition = length(setsubtract(
      [
        "ALLOWED_HOSTS", "ANTHROPIC_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_BUCKET_NAME",
        "AWS_REGION", "AWS_SECRET_ACCESS_KEY", "BACKEND_ENVIRONMENT", "BASE_URL",
        "CHATBOT_AVATAR_PROMPT", "CHATBOT_CONTROL_IMAGE", "CONN_MAX_AGE",
        "CORS_ALLOW_CREDENTIALS", "DATABASE_HOST", "DATABASE_NAME", "DATABASE_PASSWORD",
        "DATABASE_PORT", "DATABASE_USER", "DEBUG", "DEFAULT_FROM_EMAIL", "EMAIL_BACKEND",
        "GUNICORN_WORKERS", "MOCK_LLM", "MOCK_LLM_P50_MS", "MOCK_MODERATION_P50_MS",
        "OPENAI_API_KEY", "REACT_APP_API_URL", "REDIS_URL", "SECRET_KEY", "SITE_URL",
        "SURVEY_INGEST_TOKEN",
      ],
      setunion(
        regexall("(?m)^[A-Z][A-Z0-9_]*", file("../../api/env_vars.txt")),
        ["BASE_URL", "CORS_ALLOW_CREDENTIALS", "REACT_APP_API_URL"],
      ),
      )) == 0 && length(setintersection(
      ["BASE_URL", "CORS_ALLOW_CREDENTIALS", "REACT_APP_API_URL"],
      setunion(
        regexall("(?m)^[A-Z][A-Z0-9_]*", file("../../api/env_vars.txt")),
        nonsensitive([for s in aws_elastic_beanstalk_environment.chatbot_api.setting : s.name]),
      ),
    )) == 0
    error_message = "Every name the live environments set must be read by the application or listed as dropped, and a dropped name must be neither read nor set by Terraform."
  }
}

run "production_values" {
  command = plan

  # The bucket name is known only after apply; a fixed value stands in.
  override_resource {
    target          = aws_s3_bucket.application_data
    override_during = plan
    values = {
      id = "chatbotlab-production-data"
    }
  }

  assert {
    condition     = nonsensitive({ for s in aws_elastic_beanstalk_environment.chatbot_api.setting : s.name => s.value if s.namespace == "aws:elasticbeanstalk:application:environment" })["AWS_BUCKET_NAME"] == "chatbotlab-production-data"
    error_message = "AWS_BUCKET_NAME must be the Terraform data bucket: without it uploads and avatars have nowhere to go."
  }

  assert {
    condition     = nonsensitive({ for s in aws_elastic_beanstalk_environment.chatbot_api.setting : s.name => s.value if s.namespace == "aws:elasticbeanstalk:application:environment" })["SITE_URL"] == "https://bot.wwbp.org"
    error_message = "SITE_URL must be https://bot.wwbp.org: invite and password-reset links are built from it."
  }

  assert {
    condition = alltrue([
      for name in ["CORS_ALLOWED_ORIGINS", "CSRF_TRUSTED_ORIGINS"] :
      contains(split(",", nonsensitive({ for s in aws_elastic_beanstalk_environment.chatbot_api.setting : s.name => s.value if s.namespace == "aws:elasticbeanstalk:application:environment" })[name]), "https://bot.wwbp.org")
    ])
    error_message = "CORS_ALLOWED_ORIGINS and CSRF_TRUSTED_ORIGINS must include https://bot.wwbp.org, the origin browsers load the site from."
  }

  # Django under ASGI must not keep connections (technical reference, gotcha 7).
  assert {
    condition     = nonsensitive({ for s in aws_elastic_beanstalk_environment.chatbot_api.setting : s.name => s.value if s.namespace == "aws:elasticbeanstalk:application:environment" })["CONN_MAX_AGE"] == "0"
    error_message = "CONN_MAX_AGE must be 0."
  }

  # t3.small: 2 vCPU, 2 GB. Async workers, so 2 per vCPU.
  assert {
    condition     = nonsensitive({ for s in aws_elastic_beanstalk_environment.chatbot_api.setting : s.name => s.value if s.namespace == "aws:elasticbeanstalk:application:environment" })["GUNICORN_WORKERS"] == "4"
    error_message = "GUNICORN_WORKERS must be 4 for small instances."
  }

  assert {
    condition     = nonsensitive({ for s in aws_elastic_beanstalk_environment.chatbot_api.setting : s.name => s.value if s.namespace == "aws:elasticbeanstalk:application:environment" })["MOCK_LLM"] == "false"
    error_message = "MOCK_LLM must be explicitly off in production: on, participants get canned replies."
  }
}

# api/entrypoint.sh runs createsuperuser on EVERY start while
# DJANGO_SUPERUSER_PASSWORD is set. Production's database is the copied one
# with the real admins, so production must never create accounts at boot.
run "production_creates_no_account_at_boot" {
  command = plan

  assert {
    condition = length(setintersection(
      nonsensitive([for s in aws_elastic_beanstalk_environment.chatbot_api.setting : s.name]),
      ["DJANGO_SUPERUSER_USERNAME", "DJANGO_SUPERUSER_EMAIL", "DJANGO_SUPERUSER_PASSWORD"],
    )) == 0
    error_message = "Production must not set DJANGO_SUPERUSER_*: the entrypoint would create an 'admin' superuser in the real database on every start."
  }
}

run "test_stack_creates_its_admin" {
  command = plan

  variables {
    environment = "test"
  }

  assert {
    condition = nonsensitive(toset([for s in aws_elastic_beanstalk_environment.chatbot_api.setting : s.name if startswith(s.name, "DJANGO_SUPERUSER_")])) == toset([
      "DJANGO_SUPERUSER_USERNAME", "DJANGO_SUPERUSER_EMAIL", "DJANGO_SUPERUSER_PASSWORD",
    ])
    error_message = "The test stack's empty database needs the admin account the entrypoint creates."
  }
}

run "test_stack_needs_an_admin_password" {
  command = plan

  variables {
    environment          = "test"
    admin_panel_password = ""
  }

  expect_failures = [var.admin_panel_password]
}

# Production starts at one instance and scales to three
# (environments/production.tfvars; check_workflows.py checks the file).
run "production_scales_from_one_to_three" {
  command = plan

  variables {
    eb_max_instances = 3
  }

  assert {
    condition = alltrue([
      for s in aws_elastic_beanstalk_environment.chatbot_api.setting :
      s.value == { MinSize = "1", MaxSize = "3" }[s.name]
      if s.namespace == "aws:autoscaling:asg" && contains(["MinSize", "MaxSize"], s.name)
    ])
    error_message = "Production's auto-scaling group must run 1 to 3 instances."
  }
}
