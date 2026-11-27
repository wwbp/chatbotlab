# Runs offline: `terraform init -backend=false && terraform test`.
# No secret value in Terraform state or in the Elastic Beanstalk settings:
# RDS keeps the master password in Secrets Manager (manage_master_user_password),
# the application secret <prefix>-<env>/app is created and filled by an
# administrator (technical reference, "Secrets"; the apply role may only look
# it up), and the environment gets only the secrets' names, which
# api/generic_chatbot/aws_secrets.py reads at start.

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

variables {
  project_name         = "chatbotlab"
  environment          = "production"
  admin_panel_password = "test-password-not-real"
}

# The secret RDS creates is known only after apply; a fixed ARN stands in.
override_resource {
  target          = aws_db_instance.main
  override_during = plan
  values = {
    master_user_secret = [{
      secret_arn    = "arn:aws:secretsmanager:us-east-1:123456789012:secret:rds!db-0123abcd-AbCdEf"
      secret_status = "active"
      kms_key_id    = "arn:aws:kms:us-east-1:123456789012:key/aws-secretsmanager"
    }]
  }
}

run "rds_keeps_the_master_password" {
  command = plan

  assert {
    condition     = aws_db_instance.main.manage_master_user_password == true && aws_db_instance.main.password == null
    error_message = "RDS must manage the master password in Secrets Manager; Terraform must not set one (it would sit in state)."
  }
}

run "no_setting_carries_a_secret_value" {
  command = plan

  # Names that look like secrets: only the two that carry secret names.
  assert {
    condition = alltrue([
      for s in aws_elastic_beanstalk_environment.chatbot_api.setting :
      contains(["APP_SECRET_ID", "DATABASE_SECRET_ID"], s.name)
      if s.namespace == "aws:elasticbeanstalk:application:environment" && can(regex("PASSWORD|SECRET|_KEY|TOKEN", s.name))
    ])
    error_message = "A secret value is set as an Elastic Beanstalk setting (visible in its configuration and in state). Put it in the <prefix>-<env>/app secret."
  }

  assert {
    condition = nonsensitive({ for s in aws_elastic_beanstalk_environment.chatbot_api.setting : s.name => s.value if s.namespace == "aws:elasticbeanstalk:application:environment" })["APP_SECRET_ID"] == "chatbotlab-production/app"
    error_message = "APP_SECRET_ID must name the application secret, chatbotlab-production/app."
  }

  assert {
    condition = nonsensitive({ for s in aws_elastic_beanstalk_environment.chatbot_api.setting : s.name => s.value if s.namespace == "aws:elasticbeanstalk:application:environment" })["DATABASE_SECRET_ID"] == "arn:aws:secretsmanager:us-east-1:123456789012:secret:rds!db-0123abcd-AbCdEf"
    error_message = "DATABASE_SECRET_ID must be the ARN of the secret RDS keeps the master user in."
  }
}

# Looked up, not created: a plan fails while the administrator has not made it.
run "application_secret_is_looked_up_by_name" {
  command = plan

  assert {
    condition     = data.aws_secretsmanager_secret.app.name == "chatbotlab-production/app"
    error_message = "The application secret must be <prefix>-<env>/app, the names the apply role and the boundary allow."
  }
}

run "instance_role_reads_only_its_own_secrets" {
  command = plan

  assert {
    condition = jsondecode(aws_iam_role_policy.eb_instance_read_secrets.policy).Statement == [{
      Sid      = "ReadOwnSecrets"
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:chatbotlab-production/*",
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:rds!db-0123abcd-AbCdEf",
      ]
    }]
    error_message = "The instance role may read only <prefix>-<env>/* and its database's RDS-managed secret."
  }
}

run "test_stack_has_its_own_secret" {
  command = plan

  variables {
    environment = "test"
  }

  assert {
    condition     = data.aws_secretsmanager_secret.app.name == "chatbotlab-test/app"
    error_message = "The test stack must use its own secret, chatbotlab-test/app (its own keys, never production's)."
  }
}
