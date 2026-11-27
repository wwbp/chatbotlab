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
