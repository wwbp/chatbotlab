# -----------------------------------------------------------------------------
# main.tf — entry point for the main Terraform configuration
#
# This file does three things:
#   1. Declares which version of Terraform and which providers I need.
#   2. Configures the remote S3 backend so state is shared and locked (the
#      bucket and key are passed in at `terraform init` time).
#   3. Configures the AWS provider with a default region and shared tags that
#      appear on every resource I create.
#
# The backend block holds no bucket or key on purpose — they are passed in at
# `terraform init` time, which keeps account-specific values out of git:
#   terraform init -backend-config=backend.hcl \
#     -backend-config="key=<project>/<environment>/terraform.tfstate"
# Each environment has its own key, so test and production never share a
# state file.
# -----------------------------------------------------------------------------

terraform {
  # 1.11 is the first release where the S3 state lock (use_lockfile) is stable.
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.4"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Bucket, key and region are filled in at `terraform init`. The lock is set
  # here, not by the caller, so no workflow or script can run without it:
  # Terraform writes <key>.tflock next to the state file and a second run on
  # the same environment stops with a lock error.
  backend "s3" {
    use_lockfile = true
  }
}

provider "aws" {
  region = var.aws_region

  # Every AWS resource I create inherits these tags automatically.
  # This makes it easy to find all resources for this project in the console
  # and to set up cost allocation reports by environment.
  default_tags {
    tags = local.common_tags
  }
}

# CloudFront requires ACM certificates to be in us-east-1 regardless of where
# the rest of the infrastructure lives. This alias lets cloudfront.tf create
# the cert in the right region without changing the default provider region.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = local.common_tags
  }
}

# I use this data source to look up the current AWS account ID at plan time.
# Several downstream resources (IAM policies, EB service role ARNs) need it.
data "aws_caller_identity" "current" {}
