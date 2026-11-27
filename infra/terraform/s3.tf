# -----------------------------------------------------------------------------
# s3.tf — the two S3 buckets this application uses
#
# I create two completely separate buckets with different purposes:
#
#   frontend_assets
#     Holds the compiled React application (HTML, JS, CSS).
#     Private — only CloudFront can read from it via Origin Access Control.
#     No public access whatsoever.
#
#   application_data
#     Holds everything the Django backend writes: bot avatar images,
#     user file uploads, and voice audio recordings from conversations.
#     Also private — the application reads and writes via its IAM role,
#     and generates short-lived presigned URLs when the frontend needs to
#     download a file directly.
#
# Both buckets block all public access. There is no static website hosting
# configured on either — CloudFront handles delivery for the frontend,
# and presigned URLs handle delivery for application data.
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Frontend assets bucket
# -----------------------------------------------------------------------------

resource "aws_s3_bucket" "frontend_assets" {
  bucket = "${local.name_prefix}-frontend"
  # Only the test stack (variables.tf refuses it for production).
  force_destroy = var.force_destroy_buckets

  tags = {
    Name    = "${local.name_prefix}-frontend"
    Purpose = "React application static files served via CloudFront"
  }
}

resource "aws_s3_bucket_versioning" "frontend_assets" {
  bucket = aws_s3_bucket.frontend_assets.id

  versioning_configuration {
    # Versioning lets me roll back a bad frontend deployment by pointing
    # CloudFront at a previous object version. Cheap insurance.
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "frontend_assets" {
  bucket = aws_s3_bucket.frontend_assets.id

  # I block all public access. CloudFront uses Origin Access Control (OAC)
  # to authenticate with S3 using its own AWS identity — no public URLs needed.
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# -----------------------------------------------------------------------------
# Application data bucket
# -----------------------------------------------------------------------------

resource "aws_s3_bucket" "application_data" {
  bucket = "${local.name_prefix}-data"
  # Only the test stack: production's participant uploads are never deleted
  # by a destroy without the backup in destroy-infrastructure.yml.
  force_destroy = var.force_destroy_buckets

  tags = {
    Name = "${local.name_prefix}-data"
  }

  # Prevent Terraform from passing tags in the CreateBucket call (us-east-1
  # rejects inline tagging for this bucket). Tags are applied via default_tags
  # at the provider level after creation.
  lifecycle {
    ignore_changes = [tags]
  }
}

resource "aws_s3_bucket_public_access_block" "application_data" {
  bucket = aws_s3_bucket.application_data.id

  # Private — the application accesses files via its EC2 instance role,
  # not via public URLs. The frontend gets temporary presigned URLs from
  # the Django API when it needs to show an avatar or play back audio.
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# SSE-S3, as on the live production bucket. S3 applies it to new buckets by
# default; stating it keeps it from depending on that default.
resource "aws_s3_bucket_server_side_encryption_configuration" "application_data" {
  bucket = aws_s3_bucket.application_data.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Participants' raw photos (uploads/) are needed only long enough to generate
# their avatar. The app deletes each one afterwards; this catches every path
# that does not (a failed generation, an abandoned upload). Same rule as the
# live production bucket, plus two backstops: parts of an interrupted
# multipart upload (invisible to listings) and noncurrent versions, should
# versioning ever be turned on, go after a day too. S3 runs expiry
# asynchronously and rounds to the next midnight UTC: 24 to 48 hours.
resource "aws_s3_bucket_lifecycle_configuration" "application_data" {
  bucket = aws_s3_bucket.application_data.id

  rule {
    id     = "expire-raw-participant-photos"
    status = "Enabled"

    filter {
      prefix = "uploads/"
    }

    expiration {
      days = 1
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }

    noncurrent_version_expiration {
      noncurrent_days = 1
    }
  }
}

# Never versioned, as live production: with versioning, the expiry above and
# the app's delete would only add delete markers, and the photo would survive
# as a noncurrent version. "Disabled" is valid only for a bucket that has
# never been versioned, so it cannot be set back after "Enabled".
resource "aws_s3_bucket_versioning" "application_data" {
  bucket = aws_s3_bucket.application_data.id

  versioning_configuration {
    status = "Disabled"
  }
}

resource "aws_s3_bucket_cors_configuration" "application_data" {
  bucket = aws_s3_bucket.application_data.id

  cors_rule {
    # The participant's browser PUTs their photo straight to S3 with a
    # presigned URL from /api/avatar-upload/ (web/src/components/Avatar.js).
    # That is the only cross-origin call: avatars are shown with <img src>,
    # which needs no CORS, and nothing in the browser deletes or posts here.
    # Only the site itself, never a wildcard: the custom domain (live
    # production allows only https://bot.wwbp.org) and this stack's own
    # CloudFront URL, which stays reachable (the rehearsal tests through it).
    allowed_headers = ["*"]
    allowed_methods = ["PUT"]
    allowed_origins = compact([
      var.domain_name != "" ? "https://${var.domain_name}" : "",
      "https://${aws_cloudfront_distribution.frontend.domain_name}",
    ])
    expose_headers  = ["ETag"]
    max_age_seconds = 3000
  }
}
