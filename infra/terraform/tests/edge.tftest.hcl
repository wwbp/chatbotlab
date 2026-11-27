# Runs offline: `terraform init -backend=false && terraform test`.
# The edge in front of the app: CloudFront's web ACL, the HTTPS hop from
# CloudFront to the load balancer, and the custom domain's certificate and DNS
# records, matched to the live production distribution (E2Q6P4TGU4GOC, see the
# 2026-11-25 inventory, section 4).

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
  mock_data "aws_route53_zone" {
    defaults = {
      zone_id = "Z0000000000000000000"
    }
  }
}

mock_provider "aws" {
  alias = "us_east_1"
}

mock_provider "random" {}

variables {
  project_name         = "chatbotlab"
  environment          = "production"
  db_password          = "test-password-not-real"
  admin_panel_password = "test-password-not-real"
}

# The web ACL's ARN is only known after apply; a fixed one lets the plan show
# that the distribution uses it.
override_resource {
  target          = aws_wafv2_web_acl.edge
  override_during = plan
  values = {
    arn = "arn:aws:wafv2:us-east-1:123456789012:global/webacl/chatbotlab-production-edge/abc"
  }
}

# --- WAF ----------------------------------------------------------------------

# Live production's web ACL: the same managed rule groups and a per-IP rate
# limit of 300 requests in 5 minutes, all blocking.
run "production_has_the_live_web_acl" {
  command = plan

  assert {
    condition     = length(aws_wafv2_web_acl.edge) == 1 && aws_wafv2_web_acl.edge[0].scope == "CLOUDFRONT"
    error_message = "Production needs a CLOUDFRONT-scoped web ACL."
  }

  assert {
    condition     = aws_cloudfront_distribution.frontend.web_acl_id == "arn:aws:wafv2:us-east-1:123456789012:global/webacl/chatbotlab-production-edge/abc"
    error_message = "The distribution must use the web ACL."
  }

  assert {
    condition = toset([
      for r in aws_wafv2_web_acl.edge[0].rule : r.statement[0].managed_rule_group_statement[0].name
      if length(r.statement[0].managed_rule_group_statement) > 0
      ]) == toset([
      "AWSManagedRulesAntiDDoSRuleSet",
      "AWSManagedRulesAmazonIpReputationList",
      "AWSManagedRulesCommonRuleSet",
      "AWSManagedRulesKnownBadInputsRuleSet",
      "AWSManagedRulesSQLiRuleSet",
    ])
    error_message = "The web ACL must use live production's managed rule groups."
  }

  # Managed groups use their own (blocking) actions: no override to count.
  assert {
    condition = alltrue([
      for r in aws_wafv2_web_acl.edge[0].rule : length(r.override_action[0].count) == 0
      if length(r.override_action) > 0
    ])
    error_message = "No managed rule group may be overridden to count."
  }

  assert {
    condition = anytrue([
      for r in aws_wafv2_web_acl.edge[0].rule :
      length(r.statement[0].rate_based_statement) > 0
      && try(r.statement[0].rate_based_statement[0].limit == 300 && r.statement[0].rate_based_statement[0].aggregate_key_type == "IP" && length(r.action[0].block) == 1, false)
    ])
    error_message = "The web ACL must block an IP after 300 requests in 5 minutes, as live production does."
  }
}

# The web ACL costs money every month; the test stack may go without it.
run "production_cannot_turn_the_web_acl_off" {
  command = plan

  variables {
    waf_enabled = false
  }

  expect_failures = [var.waf_enabled]
}

run "test_stack_may_go_without_a_web_acl" {
  command = plan

  variables {
    environment = "test"
    waf_enabled = false
  }

  assert {
    condition     = length(aws_wafv2_web_acl.edge) == 0 && aws_cloudfront_distribution.frontend.web_acl_id == null
    error_message = "With waf_enabled = false the test stack has no web ACL."
  }
}
