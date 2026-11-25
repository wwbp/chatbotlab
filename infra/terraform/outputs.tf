# -----------------------------------------------------------------------------
# outputs.tf — values printed after `terraform apply` completes
#
# After a successful apply, Terraform prints everything defined here.
# I use this file to surface the six deploy secret values (see below for
# which secret names each environment uses), the two URLs you can open in a
# browser, and a few values useful for debugging.
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
# Deploy secret values
# Each value goes into a differently named GitHub secret per environment:
# staging.yml reads AWS_*, production.yml reads AWS_PROD_* / AWS_S3_PROD_*.
# Set only the names of the environment this stack is (setup.sh prints them,
# and the Deploy Infrastructure workflow writes them when asked). Setting a
# staging name from a production stack would point staging deploys, which
# empty the frontend bucket first, at production.
# -----------------------------------------------------------------------------

output "github_secret_AWS_ACCOUNT_ID" {
  description = "AWS account ID. Deploy secret: AWS_ACCOUNT_ID for staging, AWS_PROD_ACCOUNT_ID for production."
  value       = data.aws_caller_identity.current.account_id
}

output "github_secret_AWS_ROLE_NAME" {
  description = "IAM role GitHub Actions assumes via OIDC. Deploy secret: AWS_ROLE_NAME for staging, AWS_PROD_ROLE_NAME for production."
  value       = aws_iam_role.github_actions_deployment.name
}

output "github_secret_AWS_S3_BUCKET_NAME_FRONTEND" {
  description = "Frontend bucket. Holds the compiled React app. Deploy secret: AWS_S3_BUCKET_NAME_FRONTEND for staging, AWS_S3_PROD_BUCKET_NAME_FRONTEND for production."
  value       = aws_s3_bucket.frontend_assets.id
}

output "github_secret_AWS_CLOUDFRONT_DISTRIBUTION_ID" {
  description = "CloudFront distribution invalidated after each frontend deploy. Deploy secret: AWS_CLOUDFRONT_DISTRIBUTION_ID for staging, AWS_PROD_CLOUDFRONT_DISTRIBUTION_ID for production."
  value       = aws_cloudfront_distribution.frontend.id
}

output "github_secret_AWS_BACKEND_APPLICATION_NAME" {
  description = "Elastic Beanstalk application. Deploy secret: AWS_BACKEND_APPLICATION_NAME for staging, AWS_PROD_BACKEND_APPLICATION_NAME for production."
  value       = aws_elastic_beanstalk_application.chatbot_api.name
}

output "github_secret_AWS_BACKEND_ENVIRONMENT_NAME" {
  description = "Elastic Beanstalk environment. Deploy secret: AWS_BACKEND_ENVIRONMENT_NAME for staging, AWS_PROD_BACKEND_ENVIRONMENT_NAME for production."
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
