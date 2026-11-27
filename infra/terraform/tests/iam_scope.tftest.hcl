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
  admin_panel_password = "test-password-not-real"
  domain_name          = "chat.example.org"
  # As in the workflows: the account's OIDC provider exists already, so its
  # ARN (and every trust policy naming it) is known at plan time.
  create_github_oidc_provider = false
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
        aws_db_parameter_group.main.name,
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

# The app deploy role (<project>-<environment>-github-actions) can write the
# frontend bucket and deploy to Elastic Beanstalk. A pull request can add a
# workflow of its own that asks for an OIDC token, and no text check sees a
# new file: only the trust policy can refuse it. Production trusts only
# pushes and manual runs on main (what production.yml presents); the test
# stack only the `test` GitHub Environment. Never a wildcard subject, never
# pull_request.
run "deploy_role_trusts_production_main_only" {
  command = plan

  variables {
    environment = "production"
  }

  assert {
    condition = alltrue([
      for st in jsondecode(aws_iam_role.github_actions_deployment.assume_role_policy).Statement :
      !contains(keys(st.Condition), "StringLike")
      && toset(st.Condition.StringEquals["token.actions.githubusercontent.com:sub"]) == toset([
        "repo:wwbp/chatbotlab:ref:refs/heads/main",
        "repo:wwbp@42522115/chatbotlab@882137210:ref:refs/heads/main",
      ])
    ])
    error_message = "The production deploy role must trust exactly repo:wwbp/chatbotlab:ref:refs/heads/main (both subject forms), with no wildcard."
  }
}

run "deploy_role_trusts_test_environment_only" {
  command = plan

  assert {
    condition = alltrue([
      for st in jsondecode(aws_iam_role.github_actions_deployment.assume_role_policy).Statement :
      !contains(keys(st.Condition), "StringLike")
      && toset(st.Condition.StringEquals["token.actions.githubusercontent.com:sub"]) == toset([
        "repo:wwbp/chatbotlab:environment:test",
        "repo:wwbp@42522115/chatbotlab@882137210:environment:test",
      ])
    ])
    error_message = "The test deploy role must trust exactly the test GitHub Environment (both subject forms), with no wildcard."
  }
}

run "deploy_role_subjects_have_no_wildcard_or_pull_request" {
  command = plan

  variables {
    environment = "production"
  }

  assert {
    condition = alltrue([
      for st in jsondecode(aws_iam_role.github_actions_deployment.assume_role_policy).Statement : alltrue([
        for op, conds in st.Condition : alltrue([
          for sub in flatten([lookup(conds, "token.actions.githubusercontent.com:sub", [])]) :
          !endswith(sub, ":*") && !strcontains(sub, "*") && !strcontains(sub, "pull_request")
        ])
      ])
    ])
    error_message = "No deploy role subject may contain a wildcard or pull_request."
  }
}

# Both stacks share one AWS account, so the Environment and Project tags are
# what the apply roles' ABAC statements and cross-environment Denies key on.
# Every resource gets them from the providers' default_tags = local.common_tags
# (main.tf; tests/check_workflows.py checks both providers carry it and that
# no resource overrides the keys). A mocked plan cannot evaluate default_tags
# (tags_all stays unknown), so this checks the map they come from.
run "default_tags_name_the_environment_and_project" {
  command = plan

  assert {
    condition     = local.common_tags["Environment"] == "test" && local.common_tags["Project"] == "chatbotlab"
    error_message = "local.common_tags (the providers' default_tags) must carry Environment = var.environment and Project = var.project_name."
  }
}

run "default_tags_name_production" {
  command = plan

  variables {
    environment = "production"
  }

  assert {
    condition     = local.common_tags["Environment"] == "production" && local.common_tags["Project"] == "chatbotlab"
    error_message = "local.common_tags (the providers' default_tags) must carry Environment = var.environment and Project = var.project_name."
  }
}

# A rehearsal copies real participant data into the test stack's database,
# in the account it shares with production: it must be as private and as
# encrypted as production's.
run "test_database_is_private_and_encrypted" {
  command = plan

  assert {
    condition     = aws_db_instance.main.storage_encrypted && !aws_db_instance.main.publicly_accessible
    error_message = "The test database must be encrypted and not publicly accessible, like production's."
  }
}
