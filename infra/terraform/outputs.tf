# -----------------------------------------------------------------------------
# outputs.tf — values printed after `terraform apply` completes
#
# After a successful apply, Terraform prints everything defined here.
# I use this file to surface the six GitHub Actions secrets you need to
# update, the two URLs you can immediately open in a browser, and a few
# values useful for debugging.
#
# You can re-print outputs at any time with:
#   terraform output
# Or read a single value with:
#   terraform output -raw cloudfront_url
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# URLs — open these in a browser after the first `eb deploy`
# -----------------------------------------------------------------------------

output "frontend_url" {
  description = "The CloudFront URL where the React application is served. This is your public frontend address."
  value       = var.domain_name != "" ? "https://${var.domain_name}" : "https://${aws_cloudfront_distribution.frontend.domain_name}"
}

output "backend_api_url" {
  description = "The Elastic Beanstalk CNAME for the Django API. Health check: curl http://<this>/health/"
  value       = "http://${aws_elastic_beanstalk_environment.chatbot_api.cname}"
}

# -----------------------------------------------------------------------------
# GitHub Actions secrets
# Copy these six values into your GitHub repository secrets at:
#   https://github.com/<org>/<repo>/settings/secrets/actions
# -----------------------------------------------------------------------------

output "github_secret_AWS_ACCOUNT_ID" {
  description = "Set as GitHub secret AWS_ACCOUNT_ID"
  value       = data.aws_caller_identity.current.account_id
}

output "github_secret_AWS_ROLE_NAME" {
  description = "Set as GitHub secret AWS_ROLE_NAME — the IAM role GitHub Actions assumes via OIDC"
  value       = aws_iam_role.github_actions_deployment.name
}

output "github_secret_AWS_S3_BUCKET_NAME_FRONTEND" {
  description = "Set as GitHub secret AWS_S3_BUCKET_NAME_FRONTEND (used in staging.yml) — the S3 bucket that holds the compiled React app"
  value       = aws_s3_bucket.frontend_assets.id
}

output "github_secret_AWS_S3_PROD_BUCKET_NAME_FRONTEND" {
  description = "Set as GitHub secret AWS_S3_PROD_BUCKET_NAME_FRONTEND (used in production.yml)"
  value       = aws_s3_bucket.frontend_assets.id
}

output "github_secret_AWS_CLOUDFRONT_DISTRIBUTION_ID" {
  description = "Set as GitHub secret AWS_CLOUDFRONT_DISTRIBUTION_ID (staging.yml) — used to invalidate the CDN cache after each frontend deploy"
  value       = aws_cloudfront_distribution.frontend.id
}

output "github_secret_AWS_PROD_CLOUDFRONT_DISTRIBUTION_ID" {
  description = "Set as GitHub secret AWS_PROD_CLOUDFRONT_DISTRIBUTION_ID (production.yml)"
  value       = aws_cloudfront_distribution.frontend.id
}

output "github_secret_AWS_BACKEND_APPLICATION_NAME" {
  description = "Set as GitHub secret AWS_BACKEND_APPLICATION_NAME — the Elastic Beanstalk application name"
  value       = aws_elastic_beanstalk_application.chatbot_api.name
}

output "github_secret_AWS_BACKEND_ENVIRONMENT_NAME" {
  description = "Set as GitHub secret AWS_BACKEND_ENVIRONMENT_NAME (staging.yml)"
  value       = aws_elastic_beanstalk_environment.chatbot_api.name
}

output "github_secret_AWS_PROD_BACKEND_ENVIRONMENT_NAME" {
  description = "Set as GitHub secret AWS_PROD_BACKEND_ENVIRONMENT_NAME (production.yml)"
  value       = aws_elastic_beanstalk_environment.chatbot_api.name
}

# -----------------------------------------------------------------------------
# Infrastructure details — useful for debugging and manual verification
# -----------------------------------------------------------------------------

output "rds_endpoint" {
  description = "The RDS MariaDB hostname. Use this to connect with a MySQL client for debugging: mysql -h <endpoint> -u chatbot_user -p chatbot_db"
  value       = aws_db_instance.main.address
}

output "data_bucket_name" {
  description = "The S3 bucket where the application stores avatars, uploads, and voice audio. Check it with: aws s3 ls s3://<bucket>/avatar/"
  value       = aws_s3_bucket.application_data.id
}

output "redis_url" {
  description = "The ElastiCache Redis connection URL injected into the application. Marked sensitive — use `terraform output -raw redis_url` to display it."
  value       = local.redis_url
  sensitive   = true
}

output "eb_solution_stack" {
  description = "The exact Docker platform version EB is running. Useful to know if you need to check for platform updates."
  value       = data.aws_elastic_beanstalk_solution_stack.docker_al2023.name
}

output "cloudfront_domain_name" {
  description = "The raw CloudFront domain name (e.g. d1abc123.cloudfront.net). Use this as the CNAME target when pointing a custom domain at the chatbot."
  value       = aws_cloudfront_distribution.frontend.domain_name
}

output "acm_validation_cname_name" {
  description = "The DNS record name to add at your registrar to validate the custom domain SSL certificate. Empty when no custom domain is configured."
  value       = var.domain_name != "" ? tolist(aws_acm_certificate.custom_domain[0].domain_validation_options)[0].resource_record_name : ""
}

output "acm_validation_cname_value" {
  description = "The DNS record value to add at your registrar to validate the custom domain SSL certificate. Empty when no custom domain is configured."
  value       = var.domain_name != "" ? tolist(aws_acm_certificate.custom_domain[0].domain_validation_options)[0].resource_record_value : ""
}
