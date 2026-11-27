# Runs offline: `terraform init -backend=false && terraform test`.
# Staff invites and password resets are sent through Amazon SES by the
# instance role (api/chatbot/email_backend.py). These cases check that a
# Terraform-built stack gets the settings and the permission, and no more.
# Production sends through SES as no-reply@bot.wwbp.org unless told otherwise;
# the test stack only ever prints emails to its log, so a rehearsal cannot
# write to real participants.

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
  environment          = "production"
  admin_panel_password = "test-password-not-real"
}

run "email_settings_reach_the_application" {
  command = plan

  variables {
    site_url           = "https://dev.example.org"
    email_backend      = "chatbot.email_backend.SESBackend"
    default_from_email = "ChatbotLab (test) <no-reply@example.org>"
  }

  assert {
    condition = tomap(nonsensitive({
      for s in aws_elastic_beanstalk_environment.chatbot_api.setting :
      s.name => s.value if contains(["SITE_URL", "EMAIL_BACKEND", "DEFAULT_FROM_EMAIL"], s.name)
      })) == tomap({
      SITE_URL           = "https://dev.example.org"
      EMAIL_BACKEND      = "chatbot.email_backend.SESBackend"
      DEFAULT_FROM_EMAIL = "ChatbotLab (test) <no-reply@example.org>"
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
    default_from_email = "ChatbotLab (test) <no-reply@example.org>"
  }

  assert {
    condition     = length(aws_iam_role_policy.eb_instance_send_email) == 1 && aws_iam_role_policy.eb_instance_send_email[0].role == "chatbotlab-production-eb-instance-role"
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

run "production_sends_through_ses_by_default" {
  command = plan

  assert {
    condition = tomap(nonsensitive({
      for s in aws_elastic_beanstalk_environment.chatbot_api.setting :
      s.name => s.value if contains(["EMAIL_BACKEND", "DEFAULT_FROM_EMAIL"], s.name)
      })) == tomap({
      EMAIL_BACKEND      = "chatbot.email_backend.SESBackend"
      DEFAULT_FROM_EMAIL = "ChatbotLab <no-reply@bot.wwbp.org>"
    })
    error_message = "With no email variables, production must send through SES as ChatbotLab <no-reply@bot.wwbp.org>: no workflow passes the variables, and a stack that only logs its emails loses every invite."
  }

  assert {
    condition = (
      length(aws_iam_role_policy.eb_instance_send_email) == 1 &&
      jsondecode(aws_iam_role_policy.eb_instance_send_email[0].policy).Statement[0].Condition.StringEquals["ses:FromAddress"] == "no-reply@bot.wwbp.org" &&
      toset(jsondecode(aws_iam_role_policy.eb_instance_send_email[0].policy).Statement[0].Resource) == toset([
        "arn:aws:ses:us-east-1:123456789012:identity/no-reply@bot.wwbp.org",
        "arn:aws:ses:us-east-1:123456789012:identity/bot.wwbp.org",
      ])
    )
    error_message = "Production's instance role must be allowed to send as no-reply@bot.wwbp.org through the bot.wwbp.org identity, and nothing else."
  }
}

run "test_stack_prints_emails_to_its_log" {
  command = plan

  variables {
    environment = "test"
  }

  assert {
    condition     = nonsensitive(one([for s in aws_elastic_beanstalk_environment.chatbot_api.setting : s.value if s.name == "EMAIL_BACKEND"])) == "django.core.mail.backends.console.EmailBackend"
    error_message = "The test stack must set the console backend explicitly, so a backend set by hand cannot send to real people."
  }

  assert {
    condition     = length(aws_iam_role_policy.eb_instance_send_email) == 0
    error_message = "The test stack's instance role must not be allowed to send email."
  }
}

run "test_stack_refuses_ses" {
  command = plan

  variables {
    environment        = "test"
    email_backend      = "chatbot.email_backend.SESBackend"
    default_from_email = "no-reply@example.org"
  }

  expect_failures = [var.email_backend]
}

# Any real backend is refused on the test stack, not only SES.
run "test_stack_refuses_smtp" {
  command = plan

  variables {
    environment   = "test"
    email_backend = "django.core.mail.backends.smtp.EmailBackend"
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
    default_from_email = "ChatbotLab test"
  }

  expect_failures = [var.default_from_email]
}

# Least privilege: a sender alone, with emails still going to the log, must
# not grant permission to send.
run "a_sender_without_the_ses_backend_grants_nothing" {
  command = plan

  variables {
    email_backend      = "django.core.mail.backends.console.EmailBackend"
    default_from_email = "ChatbotLab <no-reply@example.org>"
  }

  assert {
    condition     = length(aws_iam_role_policy.eb_instance_send_email) == 0
    error_message = "The SES permission must exist only when the SES backend is selected."
  }
}
