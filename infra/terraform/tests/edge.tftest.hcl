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

# --- CloudFront to the load balancer over HTTPS ---------------------------------

# Live production reaches its load balancer over HTTPS. Here CloudFront
# connects to <prefix>-origin.<dns_zone_name>, a Route 53 name for the
# Elastic Beanstalk CNAME, and the load balancer's HTTPS listener presents an
# ACM certificate for that name.
run "cloudfront_reaches_the_load_balancer_over_https" {
  command = plan

  override_resource {
    target          = aws_acm_certificate.origin
    override_during = plan
    values = {
      arn = "arn:aws:acm:us-east-1:123456789012:certificate/origin"
      domain_validation_options = [{
        domain_name           = "chatbotlab-production-origin.wwbp.org"
        resource_record_name  = "_0123456789abcdef0123456789abcdef.chatbotlab-production-origin.wwbp.org."
        resource_record_type  = "CNAME"
        resource_record_value = "_x.acm-validations.aws."
      }]
    }
  }

  assert {
    condition = alltrue([
      for o in aws_cloudfront_distribution.frontend.origin :
      o.domain_name == "chatbotlab-production-origin.wwbp.org"
      && o.custom_origin_config[0].origin_protocol_policy == "https-only"
      && o.custom_origin_config[0].origin_ssl_protocols == toset(["TLSv1.2"])
      if length(o.custom_origin_config) > 0
    ])
    error_message = "CloudFront must reach the load balancer https-only, TLS 1.2, at <prefix>-origin.<zone>."
  }

  assert {
    condition     = aws_acm_certificate.origin.domain_name == "chatbotlab-production-origin.wwbp.org" && aws_acm_certificate.origin.validation_method == "DNS"
    error_message = "The load balancer's certificate must be for the origin name, validated by DNS."
  }

  assert {
    condition = alltrue([
      aws_route53_record.origin.name == "chatbotlab-production-origin.wwbp.org",
      aws_route53_record.origin.type == "CNAME",
      aws_route53_record.origin.zone_id == "Z0000000000000000000",
      aws_route53_record.origin_validation.zone_id == "Z0000000000000000000",
    ])
    error_message = "The origin name and its certificate's validation record go in the zone Terraform looked up."
  }

  # The HTTPS listener, and no plain HTTP listener at all.
  assert {
    condition = tomap(nonsensitive({
      for s in aws_elastic_beanstalk_environment.chatbot_api.setting :
      "${s.namespace}/${s.name}" => s.value if startswith(s.namespace, "aws:elbv2:listener:")
      })) == tomap({
      "aws:elbv2:listener:443/Protocol"            = "HTTPS"
      "aws:elbv2:listener:443/SSLCertificateArns"  = "arn:aws:acm:us-east-1:123456789012:certificate/origin"
      "aws:elbv2:listener:443/SSLPolicy"           = "ELBSecurityPolicy-TLS13-1-2-2021-06"
      "aws:elbv2:listener:default/ListenerEnabled" = "false"
    })
    error_message = "The load balancer needs an HTTPS listener with the origin certificate and TLS 1.2+, and the port 80 listener disabled."
  }
}

# --- Host header ------------------------------------------------------------------

# Live production forwards the viewer's Host (AllViewer) because its load
# balancer's certificate is for bot.wwbp.org: CloudFront checks the origin's
# certificate against the Host it sends. Here the certificate is for the
# origin name, so CloudFront must send that name (AllViewerExceptHostHeader);
# forwarding bot.wwbp.org, or the cloudfront.net name before the cutover,
# would fail the TLS check with a 502. Django needs no viewer Host: links are
# built from SITE_URL (staff_auth.py), ALLOWED_HOSTS is "*", and the CSRF
# origin check passes through CSRF_TRUSTED_ORIGINS.
run "the_origin_gets_its_own_name_as_host" {
  command = plan

  assert {
    condition = alltrue([
      for b in aws_cloudfront_distribution.frontend.ordered_cache_behavior :
      b.origin_request_policy_id == "b689b0a8-53d0-40ab-baf2-68738e2966ac"
    ])
    error_message = "Behaviours to the load balancer must use AllViewerExceptHostHeader: its certificate is for the origin name."
  }
}
