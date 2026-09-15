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
# ACM certificate for custom domain (only created when domain_name is set).
#
# CloudFront requires ACM certs to be in us-east-1 regardless of the rest of
# the infrastructure region. We use the aws.us_east_1 provider alias from
# main.tf for this resource and the validation resource below.
#
# On first deploy with a domain: the cert is created and the validation CNAME
# is printed to the workflow summary. The researcher adds the CNAME at their
# registrar. aws_acm_certificate_validation then waits (up to 30 min) for DNS
# to propagate before continuing to wire the cert into CloudFront.
# -----------------------------------------------------------------------------

resource "aws_acm_certificate" "custom_domain" {
  count             = var.domain_name != "" ? 1 : 0
  provider          = aws.us_east_1
  domain_name       = var.domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_acm_certificate_validation" "custom_domain" {
  count           = var.domain_name != "" ? 1 : 0
  provider        = aws.us_east_1
  certificate_arn = aws_acm_certificate.custom_domain[0].arn

  timeouts {
    create = "30m"
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
  aliases             = var.domain_name != "" ? [var.domain_name] : []

  origin {
    # I use the regional domain name (not the global one) to avoid redirect
    # loops that can happen when S3 returns a 307 to the regional endpoint.
    domain_name              = aws_s3_bucket.frontend_assets.bucket_regional_domain_name
    origin_id                = "S3-${aws_s3_bucket.frontend_assets.id}"
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend_assets.id
  }

  # The EB CNAME is the ALB endpoint. CloudFront routes /api/* and /ws/* here.
  # HTTP-only because the EB CNAME has no TLS cert — traffic is on AWS backbone.
  origin {
    domain_name = aws_elastic_beanstalk_environment.chatbot_api.cname
    origin_id   = "EB-${aws_elastic_beanstalk_environment.chatbot_api.name}"

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  # /api/* — Django REST API. No caching, all methods, all headers forwarded.
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
