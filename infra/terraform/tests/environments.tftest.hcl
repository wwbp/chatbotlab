# Runs offline: `terraform init -backend=false && terraform test`.
# Production and the on-demand test stack: each named and tagged after its
# environment, and only the test stack may relax production's data safety.

mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{}"
    }
  }
  # A real-looking account ID: the IAM roles' permissions boundary ARN embeds it.
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

variables {
  project_name         = "chatbotlab"
  db_password          = "test-password-not-real"
  admin_panel_password = "test-password-not-real"
}

run "test_names_and_tags" {
  command = plan

  variables {
    environment = "test"
  }

  assert {
    condition = alltrue([
      aws_elastic_beanstalk_application.chatbot_api.name == "chatbotlab-test",
      aws_elastic_beanstalk_environment.chatbot_api.name == "chatbotlab-test-env",
      aws_db_instance.main.identifier == "chatbotlab-test-database",
      aws_s3_bucket.frontend_assets.bucket == "chatbotlab-test-frontend",
      aws_s3_bucket.application_data.bucket == "chatbotlab-test-data",
      aws_iam_role.eb_instance_role.name == "chatbotlab-test-eb-instance-role",
      aws_iam_role.github_actions_deployment.name == "chatbotlab-test-github-actions",
    ])
    error_message = "Every test resource name must start with <project>-test."
  }

  assert {
    condition     = local.common_tags.Environment == "test"
    error_message = "Resources must be tagged with their environment."
  }
}

run "production_names_and_tags" {
  command = plan

  variables {
    environment = "production"
  }

  assert {
    condition = alltrue([
      aws_elastic_beanstalk_application.chatbot_api.name == "chatbotlab-production",
      aws_elastic_beanstalk_environment.chatbot_api.name == "chatbotlab-production-env",
      aws_db_instance.main.identifier == "chatbotlab-production-database",
      aws_s3_bucket.frontend_assets.bucket == "chatbotlab-production-frontend",
      aws_s3_bucket.application_data.bucket == "chatbotlab-production-data",
      aws_iam_role.eb_instance_role.name == "chatbotlab-production-eb-instance-role",
      aws_iam_role.github_actions_deployment.name == "chatbotlab-production-github-actions",
    ])
    error_message = "Every production resource name must start with <project>-production."
  }

  assert {
    condition     = local.common_tags.Environment == "production"
    error_message = "Resources must be tagged with their environment."
  }
}

# Production holds study data: backups, deletion protection, a final snapshot,
# and buckets Terraform will not delete while they hold objects. These are the
# values in environments/production.tfvars (check_workflows.py checks the file).
run "production_keeps_its_data" {
  command = plan

  variables {
    environment             = "production"
    deletion_protection     = true
    backup_retention_period = 7
    force_destroy_buckets   = false
  }

  assert {
    condition     = aws_db_instance.main.backup_retention_period >= 7 && aws_db_instance.main.deletion_protection && !aws_db_instance.main.skip_final_snapshot
    error_message = "The production database must keep at least 7 days of backups, deletion protection and a final snapshot."
  }

  assert {
    condition     = !aws_s3_bucket.frontend_assets.force_destroy && !aws_s3_bucket.application_data.force_destroy
    error_message = "Production buckets must not be force-destroyed."
  }
}

# The defaults are production's, so a stack that sets nothing keeps its data.
run "defaults_keep_data" {
  command = plan

  variables {
    environment = "test"
  }

  assert {
    condition     = aws_db_instance.main.deletion_protection && aws_db_instance.main.backup_retention_period >= 7 && !aws_s3_bucket.application_data.force_destroy
    error_message = "Without settings, a stack must keep deletion protection, 7 days of backups and non-force-destroyed buckets."
  }
}

# The on-demand test stack (environments/test.tfvars) may be cheap and
# disposable.
run "test_may_be_disposable" {
  command = plan

  variables {
    environment             = "test"
    deletion_protection     = false
    backup_retention_period = 1
    force_destroy_buckets   = true
  }

  assert {
    condition     = !aws_db_instance.main.deletion_protection && aws_db_instance.main.backup_retention_period == 1 && aws_db_instance.main.skip_final_snapshot
    error_message = "The test database may run without deletion protection, with 1 day of backups and no final snapshot."
  }

  assert {
    condition     = aws_s3_bucket.frontend_assets.force_destroy && aws_s3_bucket.application_data.force_destroy
    error_message = "The test stack's buckets may be force-destroyed."
  }
}

run "production_refuses_force_destroy" {
  command = plan

  variables {
    environment           = "production"
    force_destroy_buckets = true
  }

  expect_failures = [var.force_destroy_buckets]
}

run "production_refuses_no_deletion_protection" {
  command = plan

  variables {
    environment         = "production"
    deletion_protection = false
  }

  expect_failures = [var.deletion_protection]
}

run "production_refuses_short_backups" {
  command = plan

  variables {
    environment             = "production"
    backup_retention_period = 6
  }

  expect_failures = [var.backup_retention_period]
}

run "staging_is_retired" {
  command = plan

  variables {
    environment = "staging"
  }

  expect_failures = [var.environment]
}

run "unknown_environment_is_rejected" {
  command = plan

  variables {
    environment = "dev"
  }

  expect_failures = [var.environment]
}
