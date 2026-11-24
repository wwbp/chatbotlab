# Runs offline: `terraform init -backend=false && terraform test`.
# Staff invites and password resets are sent through Amazon SES by the
# instance role (api/chatbot/email_backend.py). These cases check that a
# Terraform-built stack gets the settings and the permission, and no more.

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

# The EB settings set also holds secrets, so Terraform marks anything derived
# from it as sensitive. The email values are not secret, hence nonsensitive().

variables {
  project_name         = "chatbotlab"
  environment          = "staging"
  db_password          = "test-password-not-real"
  admin_panel_password = "test-password-not-real"
}

run "email_settings_reach_the_application" {
  command = plan

  variables {
    site_url           = "https://dev.example.org"
    email_backend      = "chatbot.email_backend.SESBackend"
    default_from_email = "ChatbotLab (staging) <no-reply@example.org>"
  }

  assert {
    condition = tomap(nonsensitive({
      for s in aws_elastic_beanstalk_environment.chatbot_api.setting :
      s.name => s.value if contains(["SITE_URL", "EMAIL_BACKEND", "DEFAULT_FROM_EMAIL"], s.name)
      })) == tomap({
      SITE_URL           = "https://dev.example.org"
      EMAIL_BACKEND      = "chatbot.email_backend.SESBackend"
      DEFAULT_FROM_EMAIL = "ChatbotLab (staging) <no-reply@example.org>"
    })
    error_message = "SITE_URL, EMAIL_BACKEND and DEFAULT_FROM_EMAIL must be set on the EB environment from the variables."
  }

  assert {
    condition = nonsensitive(alltrue([
      for s in aws_elastic_beanstalk_environment.chatbot_api.setting :
      s.namespace == "aws:elasticbeanstalk:application:environment"
      if contains(["SITE_URL", "EMAIL_BACKEND", "DEFAULT_FROM_EMAIL"], s.name)
    ]))
    error_message = "The email settings must be application environment variables."
  }
}

run "instance_role_may_send_from_the_configured_address_only" {
  command = plan

  variables {
    email_backend      = "chatbot.email_backend.SESBackend"
    default_from_email = "ChatbotLab (staging) <no-reply@example.org>"
  }

  assert {
    condition     = length(aws_iam_role_policy.eb_instance_send_email) == 1 && aws_iam_role_policy.eb_instance_send_email[0].role == "chatbotlab-staging-eb-instance-role"
    error_message = "The SES policy must be attached to the EB instance role."
  }

  assert {
    condition     = length(jsondecode(aws_iam_role_policy.eb_instance_send_email[0].policy).Statement) == 1
    error_message = "The SES policy must hold exactly one statement."
  }

  assert {
    condition = (
      jsondecode(aws_iam_role_policy.eb_instance_send_email[0].policy).Statement[0].Effect == "Allow" &&
      toset(jsondecode(aws_iam_role_policy.eb_instance_send_email[0].policy).Statement[0].Action) == toset(["ses:SendEmail", "ses:SendRawEmail"])
    )
    error_message = "The SES statement may allow sending only: ses:SendEmail and ses:SendRawEmail."
  }

  # The bare address, as email_backend.py passes it in FromEmailAddress.
  assert {
    condition = jsondecode(aws_iam_role_policy.eb_instance_send_email[0].policy).Statement[0].Condition == {
      StringEquals = { "ses:FromAddress" = "no-reply@example.org" }
    }
    error_message = "Sending must be conditioned on ses:FromAddress equal to the configured sender, and nothing else."
  }

  assert {
    condition = toset(jsondecode(aws_iam_role_policy.eb_instance_send_email[0].policy).Statement[0].Resource) == toset([
      "arn:aws:ses:us-east-1:123456789012:identity/no-reply@example.org",
      "arn:aws:ses:us-east-1:123456789012:identity/example.org",
    ])
    error_message = "The SES statement must name only the sender's address identity and its domain identity, not every identity in the account."
  }

}

run "a_plain_sender_address_is_used_as_is" {
  command = plan

  variables {
    email_backend      = "chatbot.email_backend.SESBackend"
    default_from_email = "no-reply@example.org"
  }

  assert {
    condition     = jsondecode(aws_iam_role_policy.eb_instance_send_email[0].policy).Statement[0].Condition.StringEquals["ses:FromAddress"] == "no-reply@example.org"
    error_message = "A sender without a display name must be used unchanged in the condition."
  }
}

run "site_url_follows_the_custom_domain" {
  command = plan

  variables {
    domain_name = "chat.example.org"
  }

  assert {
    condition     = nonsensitive(one([for s in aws_elastic_beanstalk_environment.chatbot_api.setting : s.value if s.name == "SITE_URL"])) == "https://chat.example.org"
    error_message = "With no site_url, SITE_URL must be the custom domain."
  }
}

run "no_email_by_default" {
  command = plan

  assert {
    condition     = length(aws_iam_role_policy.eb_instance_send_email) == 0
    error_message = "With no sender configured the instance role must get no SES permission."
  }

  assert {
    condition = tomap(nonsensitive({
      for s in aws_elastic_beanstalk_environment.chatbot_api.setting :
      s.name => s.value if contains(["SITE_URL", "EMAIL_BACKEND", "DEFAULT_FROM_EMAIL"], s.name)
    })) == tomap({})
    error_message = "With nothing configured, no email setting may be sent: a stack whose EMAIL_BACKEND was set by hand must not be reset, and the application defaults to the console backend on its own."
  }
}

run "ses_backend_needs_a_sender" {
  command = plan

  variables {
    email_backend = "chatbot.email_backend.SESBackend"
  }

  expect_failures = [var.email_backend]
}

# The address is taken the way Python's parseaddr takes it in
# email_backend.py: the last bracketed group, without surrounding spaces.
run "a_bracketed_display_name_does_not_fool_the_parser" {
  command = plan

  variables {
    email_backend      = "chatbot.email_backend.SESBackend"
    default_from_email = "\"Lab <team@other.org>\" <no-reply@example.org>"
  }

  assert {
    condition     = jsondecode(aws_iam_role_policy.eb_instance_send_email[0].policy).Statement[0].Condition.StringEquals["ses:FromAddress"] == "no-reply@example.org"
    error_message = "The sender is the last bracketed address, not one inside a quoted display name."
  }
}

run "spaces_inside_the_brackets_are_trimmed" {
  command = plan

  variables {
    email_backend      = "chatbot.email_backend.SESBackend"
    default_from_email = "ChatbotLab < no-reply@example.org >"
  }

  assert {
    condition     = jsondecode(aws_iam_role_policy.eb_instance_send_email[0].policy).Statement[0].Condition.StringEquals["ses:FromAddress"] == "no-reply@example.org"
    error_message = "Spaces around the address inside the brackets must be removed."
  }
}

run "a_sender_that_is_not_an_address_is_rejected" {
  command = plan

  variables {
    email_backend      = "chatbot.email_backend.SESBackend"
    default_from_email = "ChatbotLab staging"
  }

  expect_failures = [var.default_from_email]
}

# Least privilege: a sender alone, with emails still going to the log, must
# not grant permission to send.
run "a_sender_without_the_ses_backend_grants_nothing" {
  command = plan

  variables {
    default_from_email = "ChatbotLab <no-reply@example.org>"
  }

  assert {
    condition     = length(aws_iam_role_policy.eb_instance_send_email) == 0
    error_message = "The SES permission must exist only when the SES backend is selected."
  }
}
