# -----------------------------------------------------------------------------
# cloudfront.tf — the CDN that serves the React frontend to end users
#
# CloudFront sits in front of the frontend S3 bucket and handles:
#   - HTTPS termination (the S3 bucket itself serves no HTTP)
#   - Global caching so files load quickly regardless of where the user is
#   - SPA routing: any URL that doesn't match a file returns index.html
#     so that React Router can handle client-side navigation
#
# I use Origin Access Control (OAC), the modern replacement for the legacy
# Origin Access Identity (OAI). OAC signs requests with SigV4 and works
# with S3 server-side encryption. I do NOT include an s3_origin_config block
# — that is the OAI pattern and conflicts with OAC.
#
# The S3 bucket policy that grants CloudFront read access is at the bottom
# of this file because it references both the bucket (from s3.tf) and the
# CloudFront distribution ARN (defined here). Keeping them together avoids
# a circular dependency that would break `terraform plan`.
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Origin Access Control — the identity CloudFront uses to authenticate
# with S3. S3 checks the request signature against this OAC's ARN.
# -----------------------------------------------------------------------------

resource "aws_cloudfront_origin_access_control" "frontend_assets" {
  name                              = "${local.name_prefix}-frontend-oac"
  description                       = "I use this OAC to give CloudFront read access to the private frontend S3 bucket without making the bucket public."
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# -----------------------------------------------------------------------------
# CloudFront distribution
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# ACM certificate for the custom domain (only when domain_name is set).
#
# CloudFront requires ACM certs in us-east-1 whatever the stack's region,
# hence the aws.us_east_1 provider. Terraform writes the validation record in
# the Route 53 zone (dns.tf) and waits for the certificate to be issued.
#
# The name itself only moves to this distribution at the cutover
# (cutover_dns, below): a CloudFront alias can be on one distribution at a
# time, and for bot.wwbp.org the live one (E2Q6P4TGU4GOC) holds it until then.
# Until cutover_dns, the certificate is attached without the alias, which is
# what lets an administrator move the alias over without downtime
# (docs/deployment/data-migration.rst, step 11).
# -----------------------------------------------------------------------------

resource "aws_acm_certificate" "custom_domain" {
  count             = var.domain_name != "" ? 1 : 0
  provider          = aws.us_east_1
  domain_name       = var.domain_name
  validation_method = "DNS"

  # Explicit as well as default_tags: the apply role may request a
  # certificate only with its Environment and Project tags.
  tags = local.common_tags

  lifecycle {
    create_before_destroy = true
  }
}

# ACM gives a name in one account the same validation record every time, so
# for bot.wwbp.org this is the record the live certificate already uses (same
# name, same value): allow_overwrite adopts it instead of failing.
#
# Terraform must never delete it: the live certificate (CloudFront
# E2Q6P4TGU4GOC and the live ALB) renews through it, and a deleted record
# makes that renewal fail silently. Before reverting domain_name or
# destroying the stack, take it out of the state instead:
#   terraform state rm 'aws_route53_record.custom_domain_validation[0]'
# (docs/deployment/data-migration.rst, step 11).
resource "aws_route53_record" "custom_domain_validation" {
  count           = var.domain_name != "" ? 1 : 0
  zone_id         = data.aws_route53_zone.main.zone_id
  name            = one(aws_acm_certificate.custom_domain[0].domain_validation_options).resource_record_name
  type            = one(aws_acm_certificate.custom_domain[0].domain_validation_options).resource_record_type
  ttl             = 300
  records         = [one(aws_acm_certificate.custom_domain[0].domain_validation_options).resource_record_value]
  allow_overwrite = true

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_acm_certificate_validation" "custom_domain" {
  count                   = var.domain_name != "" ? 1 : 0
  provider                = aws.us_east_1
  certificate_arn         = aws_acm_certificate.custom_domain[0].arn
  validation_record_fqdns = [aws_route53_record.custom_domain_validation[0].fqdn]

  timeouts {
    create = "30m"
  }
}

# The cutover: the public name's A and AAAA records point at this
# distribution. allow_overwrite replaces the records that point at the live
# distribution today.
resource "aws_route53_record" "site" {
  for_each        = var.cutover_dns ? toset(["A", "AAAA"]) : toset([])
  zone_id         = data.aws_route53_zone.main.zone_id
  name            = var.domain_name
  type            = each.key
  allow_overwrite = true

  alias {
    name                   = aws_cloudfront_distribution.frontend.domain_name
    zone_id                = aws_cloudfront_distribution.frontend.hosted_zone_id
    evaluate_target_health = false
  }
}

# SPA routing — React Router handles navigation client-side, so a visit or
# refresh on /dashboard must return index.html rather than a missing object.
#
# This used to be done with custom_error_response mapping 403 and 404 to
# /index.html with a 200. Those apply to the whole distribution, not to one
# behavior, so they also swallowed every error the API returned: a Django 403
# reached the browser as a 200 serving the React page. That silently broke
# the frontend's error handling — fetch received HTML and a success status —
# and made failures like a rejected admin login impossible to diagnose.
#
# A viewer-request function attached only to the S3 behavior rewrites the URL
# before the request is made, so nothing has to fail for SPA routing to work
# and the API's own status codes reach the browser untouched. It never runs
# for /api/*, /static/admin/* or /ws/*, which are separate behaviors.
resource "aws_cloudfront_function" "spa_router" {
  name    = "${local.name_prefix}-spa-router"
  runtime = "cloudfront-js-2.0"
  comment = "Rewrite extensionless paths to /index.html for React Router"
  publish = true
  code    = <<-JS
    function handler(event) {
      var request = event.request;
      var uri = request.uri;

      // Anything with a file extension in its last segment is a real asset
      // (/assets/index-abc123.js, /favicon.ico) — leave it alone. Everything
      // else is a client-side route, so serve the app shell and let React
      // Router read the original URL from the address bar.
      var lastSegment = uri.substring(uri.lastIndexOf('/') + 1);
      if (lastSegment.indexOf('.') === -1) {
        request.uri = '/index.html';
      }
      return request;
    }
  JS
}

resource "aws_cloudfront_distribution" "frontend" {
  enabled             = true
  is_ipv6_enabled     = true
  default_root_object = "index.html"
  price_class         = "PriceClass_100"
  comment             = "${local.name_prefix} frontend — managed by Terraform"
  aliases             = var.cutover_dns ? [var.domain_name] : []
  web_acl_id          = var.waf_enabled ? aws_wafv2_web_acl.edge[0].arn : null

  origin {
    # I use the regional domain name (not the global one) to avoid redirect
    # loops that can happen when S3 returns a 307 to the regional endpoint.
    domain_name              = aws_s3_bucket.frontend_assets.bucket_regional_domain_name
    origin_id                = "S3-${aws_s3_bucket.frontend_assets.id}"
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend_assets.id
  }

  # The load balancer, under this stack's own name (dns.tf), over HTTPS: its
  # listener presents a certificate for that name, as live production's
  # does for bot.wwbp.org. The record's name (not the CNAME) also makes
  # CloudFront wait for the record.
  origin {
    domain_name = aws_route53_record.origin.name
    origin_id   = "EB-${aws_elastic_beanstalk_environment.chatbot_api.name}"

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  # /api/* — Django REST API. No caching, all methods, all headers forwarded
  # but Host. Live production forwards Host because its load balancer's
  # certificate is for bot.wwbp.org; this one's is for the origin name, and
  # CloudFront checks the certificate against the Host it sends. Django needs
  # no viewer Host: links come from SITE_URL, ALLOWED_HOSTS is "*", and CSRF
  # checks the Origin against CSRF_TRUSTED_ORIGINS (tests/edge.tftest.hcl).
  # Managed policy IDs from https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/using-managed-cache-policies.html
  ordered_cache_behavior {
    path_pattern           = "/api/*"
    target_origin_id       = "EB-${aws_elastic_beanstalk_environment.chatbot_api.name}"
    allowed_methods        = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods         = ["GET", "HEAD"]
    viewer_protocol_policy = "redirect-to-https"
    compress               = false

    cache_policy_id          = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad" # CachingDisabled
    origin_request_policy_id = "b689b0a8-53d0-40ab-baf2-68738e2966ac" # AllViewerExceptHostHeader
  }

  # /static/admin/* — Django admin CSS/JS served by WhiteNoise on the EB instance.
  # React build files also live under /static/ (in S3), so we scope this to
  # /static/admin/* only — everything else under /static/ falls through to S3.
  ordered_cache_behavior {
    path_pattern           = "/static/admin/*"
    target_origin_id       = "EB-${aws_elastic_beanstalk_environment.chatbot_api.name}"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    viewer_protocol_policy = "redirect-to-https"
    compress               = true

    cache_policy_id          = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad" # CachingDisabled
    origin_request_policy_id = "b689b0a8-53d0-40ab-baf2-68738e2966ac" # AllViewerExceptHostHeader
  }

  # /ws/* — Django Channels WebSocket. Same policy as API; CloudFront supports
  # WebSocket natively and passes the Upgrade/Connection headers through.
  ordered_cache_behavior {
    path_pattern           = "/ws/*"
    target_origin_id       = "EB-${aws_elastic_beanstalk_environment.chatbot_api.name}"
    allowed_methods        = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods         = ["GET", "HEAD"]
    viewer_protocol_policy = "redirect-to-https"
    compress               = false

    cache_policy_id          = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad" # CachingDisabled
    origin_request_policy_id = "b689b0a8-53d0-40ab-baf2-68738e2966ac" # AllViewerExceptHostHeader
  }

  default_cache_behavior {
    target_origin_id       = "S3-${aws_s3_bucket.frontend_assets.id}"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    # SPA routing happens here, on the S3 behavior only, so that an error from
    # the API is still an error. See the function's own comment.
    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.spa_router.arn
    }

    forwarded_values {
      query_string = false
      cookies {
        forward = "none"
      }
    }

    min_ttl     = 0
    default_ttl = 3600  # 1 hour default cache for HTML
    max_ttl     = 86400 # 24 hour max cache for hashed JS/CSS assets
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = var.domain_name == ""
    acm_certificate_arn            = var.domain_name != "" ? aws_acm_certificate_validation.custom_domain[0].certificate_arn : null
    ssl_support_method             = var.domain_name != "" ? "sni-only" : null
    minimum_protocol_version       = var.domain_name != "" ? "TLSv1.2_2021" : null
  }

  tags = local.common_tags
}

# -----------------------------------------------------------------------------
# S3 bucket policy — grants CloudFront OAC read access to the frontend bucket.
#
# I put this here (not in s3.tf) because it references both the S3 bucket ARN
# from s3.tf AND the CloudFront distribution ARN from above. Terraform resolves
# cross-file references automatically, but keeping the policy next to the
# distribution makes the dependency obvious to anyone reading the code.
# -----------------------------------------------------------------------------

data "aws_iam_policy_document" "cloudfront_reads_frontend_bucket" {
  statement {
    sid    = "AllowCloudFrontOACReadAccess"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.frontend_assets.arn}/*"]

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      # I scope this to the exact distribution ARN so no other CloudFront
      # distribution in any account can read from this bucket.
      values = [aws_cloudfront_distribution.frontend.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "frontend_assets_cloudfront_only" {
  bucket = aws_s3_bucket.frontend_assets.id
  policy = data.aws_iam_policy_document.cloudfront_reads_frontend_bucket.json
}
