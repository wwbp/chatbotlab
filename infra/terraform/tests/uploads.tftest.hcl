# Runs offline: `terraform init -backend=false && terraform test`.
# The data bucket holds bot avatars (avatar/), participants' raw photos
# (uploads/) and voice audio (utterance_audio/). These cases keep the live
# production bucket's privacy rule and narrow CORS, and keep the instance role
# to what the application calls (api/chatbot/services/s3_helper.py, upload.py,
# admin.py, and S3Storage for Utterance.audio_file).

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

# Known at plan time, so the policy JSON and the CORS origin can be read.
override_resource {
  target          = aws_s3_bucket.application_data
  override_during = plan
  values = {
    arn = "arn:aws:s3:::chatbotlab-test-data"
  }
}

override_resource {
  target          = aws_cloudfront_distribution.frontend
  override_during = plan
  values = {
    domain_name = "d111111abcdef8.cloudfront.net"
  }
}

variables {
  project_name         = "chatbotlab"
  environment          = "test"
  db_password          = "test-password-not-real"
  admin_panel_password = "test-password-not-real"
}

# Live production (bot-avatar.wwbp.org) expires uploads/ after one day: the
# participant's photo is needed only to generate their avatar. The app deletes
# it on success; this rule catches every path that does not.
run "raw_participant_photos_expire_after_one_day" {
  command = plan

  assert {
    condition = length([
      for r in aws_s3_bucket_lifecycle_configuration.application_data.rule : r
      if r.status == "Enabled"
      && one(r.filter).prefix == "uploads/"
      && one(r.expiration).days == 1
    ]) == 1
    error_message = "The data bucket must expire uploads/ after 1 day (live production's privacy rule)."
  }
}

run "data_bucket_is_private_and_encrypted" {
  command = plan

  assert {
    condition = alltrue([
      aws_s3_bucket_public_access_block.application_data.block_public_acls,
      aws_s3_bucket_public_access_block.application_data.block_public_policy,
      aws_s3_bucket_public_access_block.application_data.ignore_public_acls,
      aws_s3_bucket_public_access_block.application_data.restrict_public_buckets,
    ])
    error_message = "The data bucket must block all public access."
  }

  assert {
    condition = one([
      for r in aws_s3_bucket_server_side_encryption_configuration.application_data.rule :
      one(r.apply_server_side_encryption_by_default).sse_algorithm
    ]) == "AES256"
    error_message = "The data bucket must encrypt at rest with SSE-S3 (as live production does)."
  }
}

# The browser only PUTs a participant photo to a presigned URL
# (web/src/components/Avatar.js). Avatars are shown with <img src>, which needs
# no CORS. Nothing in the browser deletes, posts or reads with fetch().
run "production_cors_is_the_site_only_with_put" {
  command = plan

  variables {
    environment = "production"
    domain_name = "bot.wwbp.org"
  }

  assert {
    condition = alltrue([
      for r in aws_s3_bucket_cors_configuration.application_data.cors_rule :
      toset(r.allowed_origins) == toset(["https://bot.wwbp.org"])
      && toset(r.allowed_methods) == toset(["PUT"])
    ])
    error_message = "Production CORS must allow only PUT from https://bot.wwbp.org."
  }
}

run "cors_never_allows_delete_or_a_wildcard_origin" {
  command = plan

  variables {
    environment = "production"
  }

  assert {
    condition = alltrue(flatten([
      for r in aws_s3_bucket_cors_configuration.application_data.cors_rule : [
        !contains(r.allowed_methods, "DELETE"),
        alltrue([for o in r.allowed_origins : !strcontains(o, "*")]),
      ]
    ]))
    error_message = "Data bucket CORS must never allow DELETE or a wildcard origin."
  }
}

run "without_a_domain_cors_names_the_cloudfront_site" {
  command = plan

  assert {
    condition = alltrue([
      for r in aws_s3_bucket_cors_configuration.application_data.cors_rule :
      toset(r.allowed_origins) == toset(["https://d111111abcdef8.cloudfront.net"])
    ])
    error_message = "Without a custom domain, CORS must name exactly the CloudFront site."
  }
}
