# -----------------------------------------------------------------------------
# dns.tf — this stack's records in the existing Route 53 zone
#
# The zone (wwbp.org) is shared: it holds the live deployment's records, SES's
# DKIM and MAIL FROM records and other projects'. Terraform only reads it and
# creates records under this stack's own names; the apply roles may change no
# other name (bootstrap/iam, OwnDnsRecords).
#
# Origin: CloudFront reaches the load balancer over HTTPS at
# <prefix>-origin.<zone>, a CNAME for the Elastic Beanstalk environment. The
# load balancer's HTTPS listener (elasticbeanstalk.tf) presents an ACM
# certificate for that name, in the load balancer's region, validated by DNS.
# -----------------------------------------------------------------------------

data "aws_route53_zone" "main" {
  name         = var.dns_zone_name
  private_zone = false
}

locals {
  origin_domain_name = "${local.name_prefix}-origin.${var.dns_zone_name}"
}

resource "aws_route53_record" "origin" {
  zone_id = data.aws_route53_zone.main.zone_id
  name    = local.origin_domain_name
  type    = "CNAME"
  ttl     = 300
  records = [aws_elastic_beanstalk_environment.chatbot_api.cname]
}

resource "aws_acm_certificate" "origin" {
  domain_name       = local.origin_domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "origin_validation" {
  zone_id = data.aws_route53_zone.main.zone_id
  name    = one(aws_acm_certificate.origin.domain_validation_options).resource_record_name
  type    = one(aws_acm_certificate.origin.domain_validation_options).resource_record_type
  ttl     = 300
  records = [one(aws_acm_certificate.origin.domain_validation_options).resource_record_value]
}

resource "aws_acm_certificate_validation" "origin" {
  certificate_arn         = aws_acm_certificate.origin.arn
  validation_record_fqdns = [aws_route53_record.origin_validation.fqdn]
}
