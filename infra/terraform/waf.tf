# -----------------------------------------------------------------------------
# waf.tf — the web ACL in front of CloudFront
#
# The same rules as the live production distribution's web ACL (the one
# CloudFront's one-click protection made, see the 2026-11-25 inventory,
# section 4): AWS's anti-DDoS, IP reputation, common, known-bad-inputs and
# SQL injection rule groups, and a per-IP rate limit of 300 requests in
# 5 minutes. Every rule blocks, as one-click protection does outside monitor
# mode.
#
# A CloudFront web ACL lives in us-east-1 whatever the stack's region. It
# costs about $11 a month plus $0.60 per million requests, so the test stack
# may turn it off (waf_enabled); production may not.
# -----------------------------------------------------------------------------

locals {
  # Managed rule groups without settings, in evaluation order (after the
  # anti-DDoS group and the rate limit).
  waf_managed_rule_groups = [
    "AWSManagedRulesAmazonIpReputationList",
    "AWSManagedRulesCommonRuleSet",
    "AWSManagedRulesKnownBadInputsRuleSet",
    "AWSManagedRulesSQLiRuleSet",
  ]
}

resource "aws_wafv2_web_acl" "edge" {
  count    = var.waf_enabled ? 1 : 0
  provider = aws.us_east_1
  name     = "${local.name_prefix}-edge"
  scope    = "CLOUDFRONT"

  default_action {
    allow {}
  }

  rule {
    name     = "AWSManagedRulesAntiDDoSRuleSet"
    priority = 0

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesAntiDDoSRuleSet"

        managed_rule_group_configs {
          aws_managed_rules_anti_ddos_rule_set {
            client_side_action_config {
              challenge {
                usage_of_action = "ENABLED"
                # A challenge answers with an HTML page: the API, the
                # WebSocket and static files are fetched by code, which cannot
                # solve it, so they are exempt (blocking still applies).
                exempt_uri_regular_expression {
                  regex_string = "^/(api|ws|static)/|\\.[a-z0-9]+$"
                }
              }
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name_prefix}-anti-ddos"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "RateLimitPerIp"
    priority = 1

    action {
      block {}
    }

    statement {
      rate_based_statement {
        # Requests per IP in the 5-minute window (evaluation_window_sec).
        limit                 = 300
        aggregate_key_type    = "IP"
        evaluation_window_sec = 300
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name_prefix}-rate-limit"
      sampled_requests_enabled   = true
    }
  }

  dynamic "rule" {
    for_each = local.waf_managed_rule_groups
    content {
      name     = rule.value
      priority = rule.key + 2

      override_action {
        none {}
      }

      statement {
        managed_rule_group_statement {
          vendor_name = "AWS"
          name        = rule.value
        }
      }

      visibility_config {
        cloudwatch_metrics_enabled = true
        metric_name                = "${local.name_prefix}-${rule.value}"
        sampled_requests_enabled   = true
      }
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${local.name_prefix}-edge"
    sampled_requests_enabled   = true
  }
}
