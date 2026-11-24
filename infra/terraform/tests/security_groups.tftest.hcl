# Runs offline: `terraform init -backend=false && terraform test`.
# The AWS provider is mocked, so this checks the plan only, with no account.

mock_provider "aws" {
  mock_data "aws_ec2_managed_prefix_list" {
    defaults = {
      id = "pl-cloudfront"
    }
  }
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{}"
    }
  }
}

# main.tf also declares an aliased aws provider for us-east-1 (CloudFront).
mock_provider "aws" {
  alias = "us_east_1"
}

mock_provider "random" {}

variables {
  project_name         = "chatbotlab"
  environment          = "staging"
  db_password          = "test-password-not-real"
  admin_panel_password = "test-password-not-real"
}

run "load_balancer_accepts_cloudfront_only" {
  command = plan

  # The access log trusts one public hop (CloudFront) in X-Forwarded-For, so
  # anything else reaching the ALB directly could forge the logged client IP.
  assert {
    condition = alltrue([
      for rule in aws_security_group.load_balancer.ingress :
      length(coalesce(rule.cidr_blocks, [])) == 0 && length(coalesce(rule.ipv6_cidr_blocks, [])) == 0
    ])
    error_message = "The load balancer must not accept traffic from any CIDR, only CloudFront."
  }

  assert {
    condition = alltrue([
      for rule in aws_security_group.load_balancer.ingress :
      toset(rule.prefix_list_ids) == toset(["pl-cloudfront"]) && rule.from_port == 80
    ])
    error_message = "Only port 80 (CloudFront's http-only origin) from the CloudFront prefix list."
  }
}
