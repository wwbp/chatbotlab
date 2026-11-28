# -----------------------------------------------------------------------------
# iam.tf — who is allowed to do what in AWS
#
# I create three sets of IAM resources:
#
#   1. EB instance role + instance profile
#      This is the identity that the Elastic Beanstalk EC2 instances assume.
#      It lets the application read/write the data S3 bucket, call AWS Bedrock
#      for AI models, and read its own secrets from Secrets Manager — all
#      without hardcoding any AWS credentials in the application code or
#      environment variables.
#
#   2. EB service role
#      This is the identity that the Elastic Beanstalk service itself uses
#      to manage health monitoring and platform updates on my behalf.
#      Required by EB since October 2024 when launch configurations were
#      deprecated in favour of launch templates.
#
#   3. GitHub Actions OIDC provider + deployment role
#      This lets GitHub Actions deploy to this infrastructure without storing
#      any long-lived AWS access keys in GitHub secrets. Instead, GitHub's
#      OIDC token is exchanged for short-lived AWS credentials at run time.
#      I scope the trust to exactly this organisation and repository so no
#      other repo can assume this role.
# -----------------------------------------------------------------------------

# =============================================================================
# 1. EB instance role — the identity the application servers run as
# =============================================================================

resource "aws_iam_role" "eb_instance_role" {
  name                 = "${local.name_prefix}-eb-instance-role"
  permissions_boundary = local.permissions_boundary_arn
  description          = "I allow EC2 instances managed by Elastic Beanstalk to access the data S3 bucket, call Bedrock AI models, and read their own secrets."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# AWS-managed policy that gives EB instances the baseline permissions they
# need: writing logs to CloudWatch, downloading app versions from S3, etc.
resource "aws_iam_role_policy_attachment" "eb_web_tier_baseline" {
  role       = aws_iam_role.eb_instance_role.name
  policy_arn = "arn:aws:iam::aws:policy/AWSElasticBeanstalkWebTier"
}

# Custom policy scoped to exactly the resources this application needs
resource "aws_iam_role_policy" "eb_instance_application_access" {
  name = "${local.name_prefix}-application-access"
  role = aws_iam_role.eb_instance_role.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Only the three prefixes the application writes: bot and participant
        # avatars (avatar/, s3_helper.py), participants' raw photos (uploads/,
        # also presigned for the browser's PUT in upload.py), and voice audio
        # (utterance_audio/, Utterance.audio_file through S3Storage).
        Sid    = "ReadWriteApplicationDataObjects"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
        ]
        Resource = [
          for prefix in ["avatar", "uploads", "utterance_audio"] :
          "${aws_s3_bucket.application_data.arn}/${prefix}/*"
        ]
      },
      {
        # get_random_image lists avatar/. S3Storage checks a name is free
        # with HeadObject, which returns 404 for a missing key only to a
        # caller that may list the bucket (403 otherwise, and the save fails).
        Sid      = "ListApplicationDataBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.application_data.arn
      },
      {
        # The application calls Bedrock's Converse and ConverseStream
        # (bedrock_engine.py, check_models, validate_bedrock_models) with
        # the instance role's credentials: no static keys. Every model it
        # offers or still serves is Anthropic, mostly through us.* cross-region
        # inference profiles. A profile call is authorised on the profile ARN
        # (in this region) and on the foundation model in whichever us-*
        # region it routes to, hence the region wildcard on foundation-model.
        # Converse/ConverseStream are authorised by InvokeModel/
        # InvokeModelWithResponseStream; the Converse* entries are kept in
        # case AWS adds them as separate actions. Never drop InvokeModel*.
        # Model access is an account setting, so no aws-marketplace actions:
        # a model never used in this account is enabled by an admin invoking
        # it once (first-call marketplace subscription), not by this role.
        # A model from another provider needs its family added here.
        Sid    = "InvokeBedrockModels"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
          "bedrock:Converse",
          "bedrock:ConverseStream",
        ]
        Resource = [
          "arn:aws:bedrock:*::foundation-model/anthropic.*",
          "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/us.anthropic.*",
        ]
      },
    ]
  })
}

# Reading this environment's secrets at start (api/generic_chatbot/aws_secrets.py):
# the application secret and any other under <project>-<environment>/
# (secrets.tf), and the master user secret RDS keeps for this database. Never
# another environment's, and never a write. A separate policy so the
# database's ARN, known only after it exists, does not hold up the others.
resource "aws_iam_role_policy" "eb_instance_read_secrets" {
  name = "${local.name_prefix}-read-secrets"
  role = aws_iam_role.eb_instance_role.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadOwnSecrets"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = [
          "arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:${local.name_prefix}/*",
          aws_db_instance.main.master_user_secret[0].secret_arn,
        ]
      },
    ]
  })
}

# Sending staff invites and password resets through Amazon SES.
#
# Created only when the SES backend is selected and a sender is configured:
# by default on production, as no-reply@bot.wwbp.org (locals.tf). The application
# (api/chatbot/email_backend.py) calls the SES v2 SendEmail API with a raw
# MIME message and passes the sender explicitly, so the one statement here
# allows sending from exactly that address and from no other. SendRawEmail is
# listed with SendEmail because SES authorises raw content under that action.
#
# Terraform does not create or verify the SES identity, and does not ask for
# the account to leave the SES sandbox: both need a person (a DNS record or a
# confirmation email, and a support request). See README, "Email".
resource "aws_iam_role_policy" "eb_instance_send_email" {
  count = local.ses_from_address != "" && local.email_backend == "chatbot.email_backend.SESBackend" ? 1 : 0

  name = "${local.name_prefix}-send-email"
  role = aws_iam_role.eb_instance_role.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SendFromConfiguredAddressOnly"
        Effect = "Allow"
        Action = ["ses:SendEmail", "ses:SendRawEmail"]
        # SES authorises a send against the address identity or its domain
        # identity, whichever is verified. Naming both, and no others, keeps
        # the instance from sending as any other identity in the account.
        Resource = [
          "arn:aws:ses:${var.aws_region}:${data.aws_caller_identity.current.account_id}:identity/${local.ses_from_address}",
          "arn:aws:ses:${var.aws_region}:${data.aws_caller_identity.current.account_id}:identity/${split("@", local.ses_from_address)[1]}",
        ]
        Condition = {
          StringEquals = { "ses:FromAddress" = local.ses_from_address }
        }
      },
    ]
  })
}

# The instance profile is what actually gets attached to EC2 instances.
# An IAM role cannot be attached directly — it must be wrapped in a profile.
resource "aws_iam_instance_profile" "eb_instance_profile" {
  name = "${local.name_prefix}-eb-instance-profile"
  role = aws_iam_role.eb_instance_role.name
}

# =============================================================================
# 2. EB service role — the identity the Elastic Beanstalk service uses
# =============================================================================

resource "aws_iam_role" "eb_service_role" {
  name                 = "${local.name_prefix}-eb-service-role"
  permissions_boundary = local.permissions_boundary_arn
  description          = "I allow the Elastic Beanstalk service to monitor instance health and apply platform updates on my behalf."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "elasticbeanstalk.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "eb_service_enhanced_health" {
  role       = aws_iam_role.eb_service_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSElasticBeanstalkEnhancedHealth"
}

resource "aws_iam_role_policy_attachment" "eb_service_managed_updates" {
  role       = aws_iam_role.eb_service_role.name
  policy_arn = "arn:aws:iam::aws:policy/AWSElasticBeanstalkManagedUpdatesCustomerRolePolicy"
}

# =============================================================================
# 3. GitHub Actions OIDC — passwordless CI/CD deployment
# =============================================================================

# I register GitHub's OIDC provider with AWS once. GitHub Actions workflows
# request a token from this provider, then exchange it for temporary AWS
# credentials by assuming the deployment role below.
#
# Note on thumbprints: AWS now validates OIDC tokens using its own root CA
# library (changed July 2023), so these thumbprints are no longer used for
# security enforcement. They are still required as a non-empty list by the
# Terraform resource schema — I include both known values to avoid breakage
# if AWS re-enables thumbprint validation in a future update.
resource "aws_iam_openid_connect_provider" "github_actions" {
  count = var.create_github_oidc_provider ? 1 : 0

  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  thumbprint_list = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
  ]
}

locals {
  # When create_github_oidc_provider = false the provider already exists in
  # the account and I reference it by its well-known ARN instead of creating
  # a new one. This prevents a duplicate-resource error on shared accounts.
  github_oidc_provider_arn = var.create_github_oidc_provider ? aws_iam_openid_connect_provider.github_actions[0].arn : "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"
}

locals {
  deploy_subject_context = var.environment == "production" ? "ref:refs/heads/main" : "environment:test"
}

# The IAM role that GitHub Actions workflows assume during deployments
resource "aws_iam_role" "github_actions_deployment" {
  name                 = "${local.name_prefix}-github-actions"
  permissions_boundary = local.permissions_boundary_arn
  description          = "I allow GitHub Actions workflows in ${var.github_org}/${var.github_repo} to deploy the frontend to S3, invalidate CloudFront, and deploy the backend to Elastic Beanstalk."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = local.github_oidc_provider_arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          # Exactly one caller, never a wildcard: a workflow added by a pull
          # request could otherwise ask for a token and deploy with this role.
          #   production: pushes and manual runs on main (production.yml)
          #   test:       jobs in the `test` GitHub Environment (approved)
          #
          # Both subject formats are listed because GitHub issues two: the
          # classic "repo:<org>/<repo>:<context>" and, with immutable numeric
          # IDs, "repo:<org>@<org_id>/<repo>@<repo_id>:<context>" (so a trust
          # policy cannot be satisfied by re-creating a deleted org or repo
          # under the same name). A policy written only against the classic
          # format stops matching once the immutable claim is issued, and
          # every deploy fails with "Not authorized to perform
          # sts:AssumeRoleWithWebIdentity". That outage happened here; keep
          # both. Find the IDs with gh api orgs/<org> --jq .id and
          # gh api repos/<org>/<repo> --jq .id.
          "token.actions.githubusercontent.com:sub" = compact([
            "repo:${var.github_org}/${var.github_repo}:${local.deploy_subject_context}",
            var.github_org_id != "" && var.github_repo_id != "" ? "repo:${var.github_org}@${var.github_org_id}/${var.github_repo}@${var.github_repo_id}:${local.deploy_subject_context}" : "",
          ])
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_actions_deployment_permissions" {
  name = "${local.name_prefix}-github-actions-permissions"
  role = aws_iam_role.github_actions_deployment.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Deploy the compiled React app — sync build output to S3
        Sid    = "DeployFrontendToS3"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:DeleteObject",
          "s3:ListBucket",
          "s3:GetBucketLocation",
        ]
        Resource = [
          aws_s3_bucket.frontend_assets.arn,
          "${aws_s3_bucket.frontend_assets.arn}/*",
        ]
      },
      {
        # After uploading new frontend files, invalidate CloudFront's cache
        # so users immediately see the new version instead of cached old files.
        Sid      = "InvalidateFrontendCDNCache"
        Effect   = "Allow"
        Action   = ["cloudfront:CreateInvalidation"]
        Resource = aws_cloudfront_distribution.frontend.arn
      },
    ]
  })
}

# Elastic Beanstalk deployment permissions.
#
# `eb deploy` drives a rolling update, so it acts on far more than Elastic
# Beanstalk's own API: it uploads a bundle to the account-wide EB S3 bucket,
# updates a CloudFormation stack, and suspends and resumes Auto Scaling
# processes while instances are replaced. Many of the actions involved accept
# no resource ARN at all, so they cannot be scoped.
#
# This used to be hand-enumerated inline, which meant every deploy discovered
# one more missing action — elasticbeanstalk:ListPlatformBranches, then
# CreateStorageLocation, then autoscaling:SuspendProcesses — each one a failed
# deploy. AWS publishes a managed policy for exactly this principal, so use it
# rather than maintaining that list by trial and error. The inline policy above
# keeps the two grants that are genuinely ours to scope: writing the frontend
# bucket and invalidating our distribution.
resource "aws_iam_role_policy_attachment" "github_actions_elastic_beanstalk" {
  role       = aws_iam_role.github_actions_deployment.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess-AWSElasticBeanstalk"
}
