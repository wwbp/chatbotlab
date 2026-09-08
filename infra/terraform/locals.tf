# -----------------------------------------------------------------------------
# locals.tf — computed values I reuse across all other files
#
# Rather than repeating the same string concatenation or tag map in every
# resource, I define them once here. If the naming convention ever needs to
# change, I only update this file.
# -----------------------------------------------------------------------------

locals {
  # Every AWS resource I create is prefixed with this value.
  # Example: "humanlike-chatbot-staging"
  name_prefix = "${var.project_name}-${var.environment}"

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

  # Use the caller-supplied key if provided; otherwise use the auto-generated
  # one from random_password.django_secret_key (stored in state, stable across
  # re-deploys so active user sessions are never invalidated).
  django_secret_key = var.django_secret_key != "" ? var.django_secret_key : random_password.django_secret_key.result
}
