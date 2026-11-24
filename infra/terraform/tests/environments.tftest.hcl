# Runs offline: `terraform init -backend=false && terraform test`.
# Staging and production live in one AWS account, so nothing may share a name.

mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{}"
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

run "staging_names_and_tags" {
  command = plan

  variables {
    environment = "staging"
  }

  assert {
    condition = alltrue([
      aws_elastic_beanstalk_application.chatbot_api.name == "chatbotlab-staging",
      aws_elastic_beanstalk_environment.chatbot_api.name == "chatbotlab-staging-env",
      aws_db_instance.main.identifier == "chatbotlab-staging-database",
      aws_s3_bucket.frontend_assets.bucket == "chatbotlab-staging-frontend",
      aws_s3_bucket.application_data.bucket == "chatbotlab-staging-data",
      aws_iam_role.eb_instance_role.name == "chatbotlab-staging-eb-instance-role",
      aws_iam_role.github_actions_deployment.name == "chatbotlab-staging-github-actions",
    ])
    error_message = "Every staging resource name must start with <project>-staging."
  }

  assert {
    condition     = local.common_tags.Environment == "staging"
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

  # Production holds study data: it must keep backups and resist deletion.
  assert {
    condition     = aws_db_instance.main.backup_retention_period == 7 && aws_db_instance.main.deletion_protection && !aws_db_instance.main.skip_final_snapshot
    error_message = "The production database must keep 7 days of backups, deletion protection and a final snapshot."
  }
}

run "unknown_environment_is_rejected" {
  command = plan

  variables {
    environment = "dev"
  }

  expect_failures = [var.environment]
}
