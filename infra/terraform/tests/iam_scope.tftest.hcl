# Runs offline: `terraform init -backend=false && terraform test`.
# The apply roles in bootstrap/iam/ are scoped by name (<project>-<environment>-*)
# and by the Environment tag, and may create roles only under the permissions
# boundary <project>-terraform-boundary-<environment>. These runs check that the
# stack's resources fit inside that scope.

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
  environment          = "test"
  db_password          = "test-password-not-real"
  admin_panel_password = "test-password-not-real"
  domain_name          = "chat.example.org"
}

run "roles_always_carry_the_boundary" {
  command = plan

  assert {
    # Attribute by attribute: iterating over whole role objects drags
    # sensitive marks into the failure message and crashes newer Terraform.
    condition = alltrue([
      for arn in [
        aws_iam_role.eb_instance_role.permissions_boundary,
        aws_iam_role.eb_service_role.permissions_boundary,
        aws_iam_role.github_actions_deployment.permissions_boundary,
      ] : arn == "arn:aws:iam::123456789012:policy/chatbotlab-terraform-boundary-test"
    ])
    error_message = "Every IAM role must carry the chatbotlab-terraform-boundary-test permissions boundary."
  }
}

run "names_fit_the_apply_role_scope" {
  command = plan

  assert {
    condition = alltrue([
      for name in [
        aws_db_instance.main.identifier,
        aws_db_subnet_group.database.name,
        aws_db_parameter_group.mariadb.name,
        aws_elasticache_serverless_cache.redis.name,
        aws_s3_bucket.frontend_assets.bucket,
        aws_s3_bucket.application_data.bucket,
        aws_iam_role.eb_instance_role.name,
        aws_iam_role.eb_service_role.name,
        aws_iam_role.github_actions_deployment.name,
        aws_iam_instance_profile.eb_instance_profile.name,
        aws_elastic_beanstalk_environment.chatbot_api.name,
      ] : startswith(name, "chatbotlab-test-")
    ]) && aws_elastic_beanstalk_application.chatbot_api.name == "chatbotlab-test"
    error_message = "A resource name falls outside <project>-<environment>-*, which the apply role cannot manage."
  }
}

run "certificate_carries_the_environment_tag" {
  command = plan

  assert {
    condition = alltrue([
      aws_acm_certificate.custom_domain[0].tags["Environment"] == "test",
      aws_acm_certificate.custom_domain[0].tags["Project"] == "chatbotlab",
    ])
    error_message = "The ACM certificate (us-east-1 provider, no default_tags) must carry the Environment and Project tags."
  }
}
