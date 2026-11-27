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

  # Avatars and voice audio are study data: no rule may expire them.
  assert {
    condition = alltrue([
      for r in aws_s3_bucket_lifecycle_configuration.application_data.rule :
      one(r.filter).prefix == "uploads/"
      if r.status == "Enabled" && length(r.expiration) > 0
    ])
    error_message = "Only uploads/ may expire: every enabled rule with an expiration must filter on exactly uploads/."
  }

  # Parts of an interrupted multipart upload (boto3 goes multipart above 8 MB)
  # are invisible to listings and would otherwise stay forever.
  assert {
    condition = length([
      for r in aws_s3_bucket_lifecycle_configuration.application_data.rule : r
      if r.status == "Enabled" && one(r.filter).prefix == "uploads/"
      && try(one(r.abort_incomplete_multipart_upload).days_after_initiation, 0) == 1
      && try(one(r.noncurrent_version_expiration).noncurrent_days, 0) == 1
    ]) == 1
    error_message = "The uploads/ rule must also abort incomplete multipart uploads and expire noncurrent versions after 1 day."
  }
}

# With versioning, the expiry and the app's delete leave the photo behind as a
# noncurrent version. "Disabled" (never versioned) rather than "Suspended".
run "data_bucket_is_never_versioned" {
  command = plan

  assert {
    condition     = one(aws_s3_bucket_versioning.application_data.versioning_configuration).status == "Disabled"
    error_message = "The data bucket must never be versioned (status Disabled)."
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
      toset(r.allowed_origins) == toset(["https://bot.wwbp.org", "https://d111111abcdef8.cloudfront.net"])
      && toset(r.allowed_methods) == toset(["PUT"])
    ])
    error_message = "Production CORS must allow only PUT, from https://bot.wwbp.org and the stack's own CloudFront URL (still reachable, e.g. in the rehearsal)."
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

# Object actions on the three prefixes the app writes; listing on the bucket
# (get_random_image lists avatar/, and S3Storage needs it so a missing key is a
# 404 rather than a 403). Nothing else, nowhere else.
run "instance_role_s3_access_is_the_data_bucket_prefixes_only" {
  command = plan

  assert {
    condition = alltrue([
      for st in jsondecode(aws_iam_role_policy.eb_instance_application_access.policy).Statement :
      alltrue([for a in flatten([st.Action]) : !startswith(a, "s3:") || contains([
        "s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket",
      ], a)])
    ])
    error_message = "The instance role may use only s3:GetObject, PutObject, DeleteObject and ListBucket."
  }

  assert {
    condition = toset(flatten([
      for st in jsondecode(aws_iam_role_policy.eb_instance_application_access.policy).Statement :
      flatten([st.Resource]) if anytrue([for a in flatten([st.Action]) : contains(["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], a)])
      ])) == toset([
      "arn:aws:s3:::chatbotlab-test-data/avatar/*",
      "arn:aws:s3:::chatbotlab-test-data/uploads/*",
      "arn:aws:s3:::chatbotlab-test-data/utterance_audio/*",
    ])
    error_message = "Object actions must cover exactly avatar/, uploads/ and utterance_audio/ in the data bucket."
  }

  assert {
    condition = toset(flatten([
      for st in jsondecode(aws_iam_role_policy.eb_instance_application_access.policy).Statement :
      flatten([st.Resource]) if contains(flatten([st.Action]), "s3:ListBucket")
    ])) == toset(["arn:aws:s3:::chatbotlab-test-data"])
    error_message = "s3:ListBucket must be granted on the data bucket only."
  }
}
