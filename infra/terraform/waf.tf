# -----------------------------------------------------------------------------
# waf.tf — the web ACL in front of CloudFront
#
# The same rules as the live production distribution's web ACL (read
# 2026-11-27), in the same order: AWS's anti-DDoS group with live's settings,
# a per-IP rate rule of 300 requests in 5 minutes that only counts (a lab
# sharing one IP can pass that), then the IP reputation, common (its 8 KB
# request body limit, SizeRestrictions_BODY, counted rather than blocked, so
# long chat messages and survey payloads get through), known-bad-inputs and
# SQL injection groups with their own blocking actions.
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
            sensitivity_to_block = "LOW"
            client_side_action_config {
              challenge {
                usage_of_action = "ENABLED"
                sensitivity     = "HIGH"
                # A challenge answers with an HTML page that only a browser
                # loading a page can solve. Live exempts the API and files;
                # this stack also serves the WebSocket (/ws/*) and the Django
                # admin's static files (/static/admin/*, e.g. .svg icons
                # live's list misses) through CloudFront, both fetched by code.
                exempt_uri_regular_expression {
                  regex_string = "\\/api\\/|\\/ws\\/|\\/static\\/|\\.(acc|avi|css|gif|ico|jpe?g|js|json|mp[34]|ogg|otf|pdf|png|tiff?|ttf|webm|webp|woff2?|xml)$"
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
      count {}
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

          dynamic "rule_action_override" {
            for_each = rule.value == "AWSManagedRulesCommonRuleSet" ? ["SizeRestrictions_BODY"] : []
            content {
              name = rule_action_override.value
              action_to_use {
                count {}
              }
            }
          }
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
