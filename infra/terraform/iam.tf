# -----------------------------------------------------------------------------
# iam.tf — who is allowed to do what in AWS
#
# I create three sets of IAM resources:
#
#   1. EB instance role + instance profile
#      This is the identity that the Elastic Beanstalk EC2 instances assume.
#      It lets the application read/write the data S3 bucket, call AWS Bedrock
#      for AI models, and read SSM parameters — all without hardcoding any
#      AWS credentials in the application code or environment variables.
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
  name        = "${local.name_prefix}-eb-instance-role"
  description = "I allow EC2 instances managed by Elastic Beanstalk to access the data S3 bucket, call Bedrock AI models, and read SSM parameters."

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
        # The application reads and writes avatar images, user uploads, and
        # voice audio recordings to this bucket. ListBucket is needed to
        # check whether a file exists before uploading.
        Sid    = "ReadWriteApplicationDataBucket"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket",
        ]
        Resource = [
          aws_s3_bucket.application_data.arn,
          "${aws_s3_bucket.application_data.arn}/*",
        ]
      },
      {
        # The application can call AWS Bedrock to run Llama and Claude models
        # hosted by AWS. The instance role handles authentication so no API
        # keys are needed for Bedrock — only for OpenAI and Anthropic.
        Sid    = "InvokeBedrockModels"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ]
        Resource = "*"
      },
      {
        # I allow the application to read SSM parameters under its namespace.
        # This is a hook for future secrets management — right now secrets come
        # in as environment variables, but SSM is ready if needed.
        Sid    = "ReadApplicationSecrets"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:GetParameters",
          "ssm:GetParametersByPath",
        ]
        Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${local.name_prefix}/*"
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
  name        = "${local.name_prefix}-eb-service-role"
  description = "I allow the Elastic Beanstalk service to monitor instance health and apply platform updates on my behalf."

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

# The IAM role that GitHub Actions workflows assume during deployments
resource "aws_iam_role" "github_actions_deployment" {
  name        = "${local.name_prefix}-github-actions"
  description = "I allow GitHub Actions workflows in ${var.github_org}/${var.github_repo} to deploy the frontend to S3, invalidate CloudFront, and deploy the backend to Elastic Beanstalk."

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
        }
        StringLike = {
          # Both subject formats are listed because GitHub issues two.
          #
          # The classic claim is "repo:<org>/<repo>:ref:refs/heads/<branch>".
          # GitHub now also issues subjects carrying immutable numeric IDs:
          # "repo:<org>@<org_id>/<repo>@<repo_id>:ref:refs/heads/<branch>",
          # so that a trust policy cannot be satisfied by re-creating a
          # deleted org or repo under the same name.
          #
          # A pattern written only against the classic format stops matching
          # the moment the immutable claim is issued, and every deploy fails
          # with "Not authorized to perform sts:AssumeRoleWithWebIdentity" —
          # which reads like a permissions problem rather than a claim-format
          # one. That outage happened here; keep both patterns.
          #
          # Set github_org_id / github_repo_id to enable the immutable pattern.
          # Find them with:
          #   gh api orgs/<org> --jq .id
          #   gh api repos/<org>/<repo> --jq .id
          #
          # Tighten either pattern to a single branch (for example
          # ":ref:refs/heads/main") if only main should reach production.
          "token.actions.githubusercontent.com:sub" = compact([
            "repo:${var.github_org}/${var.github_repo}:*",
            var.github_org_id != "" && var.github_repo_id != "" ? "repo:${var.github_org}@${var.github_org_id}/${var.github_repo}@${var.github_repo_id}:*" : "",
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
