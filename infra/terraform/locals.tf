# -----------------------------------------------------------------------------
# locals.tf — computed values I reuse across all other files
#
# Rather than repeating the same string concatenation or tag map in every
# resource, I define them once here. If the naming convention ever needs to
# change, I only update this file.
# -----------------------------------------------------------------------------

locals {
  # Every AWS resource I create is prefixed with this value.
  # Example: "humanlike-chatbot-production"
  name_prefix = "${var.project_name}-${var.environment}"

  # Every role this stack creates carries this boundary (a customer managed
  # policy made once from bootstrap/iam/boundary-<environment>.json). The
  # apply roles may create or change roles only with it attached.
  permissions_boundary_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:policy/${var.project_name}-terraform-boundary-${var.environment}"

  # These tags appear on every resource via the provider default_tags block
  # in main.tf. They make cost reports and console searches much easier.
  common_tags = {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "terraform"
  }

  # The two availability zones I spread resources across for redundancy.
  # Using a and b of the chosen region keeps things simple without needing
  # to dynamically fetch AZ names.
  availability_zones = [
    "${var.aws_region}a",
    "${var.aws_region}b",
  ]

  # Origins the browser actually loads the app from, for Django's
  # CSRF_TRUSTED_ORIGINS and CORS_ALLOWED_ORIGINS. The CloudFront domain is
  # matched by wildcard rather than named directly, because CloudFront's
  # origin is the Elastic Beanstalk environment and naming it here would make
  # the two resources depend on each other.
  frontend_origins = join(",", compact([
    var.domain_name != "" ? "https://${var.domain_name}" : "",
    "https://*.cloudfront.net",
  ]))

  # Where invite and password-reset links point. This cannot fall back to the
  # CloudFront domain: CloudFront's origin is the Elastic Beanstalk
  # environment, so naming the distribution in an EB setting is a cycle.
  site_url = var.site_url != "" ? var.site_url : (var.domain_name != "" ? "https://${var.domain_name}" : "")

  # The bare address out of "Name <address>". api/chatbot/email_backend.py
  # passes exactly this to SES as FromEmailAddress, and the IAM policy in
  # iam.tf conditions on it.
  # The last bracketed group, trimmed, as Python's parseaddr reads it.
  ses_from_address = trimspace(try(regex("<([^<>]*)>\\s*$", var.default_from_email)[0], var.default_from_email))

  # Use the caller-supplied key if provided; otherwise use the auto-generated
  # one from random_password.django_secret_key (stored in state, stable across
  # re-deploys so active user sessions are never invalidated).
  django_secret_key = var.django_secret_key != "" ? var.django_secret_key : random_password.django_secret_key.result
}
