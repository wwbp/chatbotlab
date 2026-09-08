# -----------------------------------------------------------------------------
# elasticbeanstalk.tf — the application server that runs the Django backend
#
# Elastic Beanstalk manages the EC2 instances, load balancer, auto-scaling
# group, and health monitoring for me. I give it a Docker platform so it runs
# the existing Dockerfile from the generic_chatbot/ directory.
#
# I inject every environment variable the application needs directly into the
# EB environment configuration. Terraform resolves all the values (database
# endpoint, Redis URL, S3 bucket name) from the other resources in this
# configuration, so there is nothing to copy and paste manually.
#
# Important: Terraform provisions the infrastructure here. The application
# code is deployed separately by the GitHub Actions workflow
# (.github/workflows/staging.yml / production.yml). On first apply, EB will
# be in a "No Data" health state until the first `eb deploy` runs — that is
# expected and not an error.
#
# Key launch template note (October 2024 change):
#   AWS no longer allows new launch configurations. I set DisableIMDSv1=true
#   which signals EB to use a launch template instead. Without this flag,
#   EB silently tries to create a launch configuration and fails on new accounts.
# -----------------------------------------------------------------------------

# I look up the latest Docker platform version automatically so I never need
# to hardcode a version string that becomes stale after an AWS platform update.
data "aws_elastic_beanstalk_solution_stack" "docker_al2023" {
  most_recent = true
  name_regex  = "^64bit Amazon Linux 2023 .* running Docker$"
}

# -----------------------------------------------------------------------------
# EB Application — the logical container for all deployments
# -----------------------------------------------------------------------------

resource "aws_elastic_beanstalk_application" "chatbot_api" {
  name        = local.name_prefix
  description = "I hold all deployed versions of the ChatbotLab Django API. The Elastic Beanstalk environment below runs the active version."
}

# -----------------------------------------------------------------------------
# EB Environment — the running infrastructure (EC2, ALB, auto-scaling)
# -----------------------------------------------------------------------------

resource "aws_elastic_beanstalk_environment" "chatbot_api" {
  name                = "${local.name_prefix}-env"
  application         = aws_elastic_beanstalk_application.chatbot_api.name
  solution_stack_name = data.aws_elastic_beanstalk_solution_stack.docker_al2023.name
  tier                = "WebServer"

  # ----- VPC placement -------------------------------------------------------
  # I put the load balancer and instances in the public subnets so they can
  # reach the internet (to pull Docker images, call OpenAI/Anthropic, etc.)
  # without paying for a NAT Gateway. The database and cache are in private
  # subnets and reachable only via security group rules.

  setting {
    namespace = "aws:ec2:vpc"
    name      = "VPCId"
    value     = aws_vpc.main.id
  }
  setting {
    namespace = "aws:ec2:vpc"
    name      = "Subnets"
    value     = join(",", aws_subnet.public[*].id)
  }
  setting {
    namespace = "aws:ec2:vpc"
    name      = "ELBSubnets"
    value     = join(",", aws_subnet.public[*].id)
  }
  setting {
    namespace = "aws:ec2:vpc"
    name      = "AssociatePublicIpAddress"
    value     = "true"
  }

  # ----- Load balancer -------------------------------------------------------
  setting {
    namespace = "aws:elasticbeanstalk:environment"
    name      = "LoadBalancerType"
    value     = "application"
  }
  setting {
    namespace = "aws:elasticbeanstalk:environment"
    name      = "ServiceRole"
    value     = aws_iam_role.eb_service_role.arn
  }
  # Assign the load_balancer security group to the ALB so only the ports I
  # defined in security_groups.tf are open to the internet.
  setting {
    namespace = "aws:elbv2:loadbalancer"
    name      = "SecurityGroups"
    value     = aws_security_group.load_balancer.id
  }

  # ----- Instances -----------------------------------------------------------
  # DisableIMDSv1=true is the flag that tells EB to use a launch template
  # instead of a launch configuration. Required on all accounts created after
  # October 2024 (and best practice on older accounts too).
  setting {
    namespace = "aws:autoscaling:launchconfiguration"
    name      = "DisableIMDSv1"
    value     = "true"
  }
  setting {
    namespace = "aws:ec2:instances"
    name      = "InstanceTypes"
    value     = var.eb_instance_type
  }
  setting {
    namespace = "aws:autoscaling:launchconfiguration"
    name      = "IamInstanceProfile"
    value     = aws_iam_instance_profile.eb_instance_profile.name
  }
  setting {
    namespace = "aws:autoscaling:launchconfiguration"
    name      = "SecurityGroups"
    value     = aws_security_group.app_instances.id
  }

  # ----- Auto scaling --------------------------------------------------------
  setting {
    namespace = "aws:autoscaling:asg"
    name      = "MinSize"
    value     = "1"
  }
  setting {
    namespace = "aws:autoscaling:asg"
    name      = "MaxSize"
    # Caller-controlled: 1 is plenty for most studies, raise it if hundreds of
    # conversations run at once.
    value = tostring(var.eb_max_instances)
  }

  # ----- Health check --------------------------------------------------------
  # /health/ is a lightweight endpoint in chatbot/urls.py that returns 200
  # without touching the database. EB uses it to decide if an instance is
  # ready to receive traffic.
  setting {
    namespace = "aws:elasticbeanstalk:application"
    name      = "Application Healthcheck URL"
    value     = "/health/"
  }
  setting {
    namespace = "aws:elasticbeanstalk:healthreporting:system"
    name      = "SystemType"
    value     = "enhanced"
  }

  # ===========================================================================
  # Application environment variables
  # All secrets and endpoints are injected here so the application code never
  # needs to know where it is running — it just reads os.getenv().
  # ===========================================================================

  # ----- Django core ---------------------------------------------------------
  setting {
    namespace = "aws:elasticbeanstalk:application:environment"
    name      = "DEBUG"
    value     = "False"
  }
  setting {
    namespace = "aws:elasticbeanstalk:application:environment"
    name      = "BACKEND_ENVIRONMENT"
    value     = var.environment
  }
  setting {
    namespace = "aws:elasticbeanstalk:application:environment"
    name      = "SECRET_KEY"
    value     = local.django_secret_key
  }
  setting {
    namespace = "aws:elasticbeanstalk:application:environment"
    name      = "ALLOWED_HOSTS"
    # Wildcard lets the EB health checker reach the app without knowing the
    # exact hostname. Tighten this to your domain after DNS is configured.
    value = "*"
  }

  # ----- Database ------------------------------------------------------------
  setting {
    namespace = "aws:elasticbeanstalk:application:environment"
    name      = "DATABASE_ENGINE"
    value     = "django.db.backends.mysql"
  }
  setting {
    namespace = "aws:elasticbeanstalk:application:environment"
    name      = "DATABASE_HOST"
    value     = aws_db_instance.main.address
  }
  setting {
    namespace = "aws:elasticbeanstalk:application:environment"
    name      = "DATABASE_PORT"
    value     = "3306"
  }
  setting {
    namespace = "aws:elasticbeanstalk:application:environment"
    name      = "DATABASE_NAME"
    value     = "chatbot_db"
  }
  setting {
    namespace = "aws:elasticbeanstalk:application:environment"
    name      = "DATABASE_USER"
    value     = "chatbot_user"
  }
  setting {
    namespace = "aws:elasticbeanstalk:application:environment"
    name      = "DATABASE_PASSWORD"
    value     = var.db_password
  }

  # ----- Cache ---------------------------------------------------------------
  setting {
    namespace = "aws:elasticbeanstalk:application:environment"
    name      = "REDIS_URL"
    # The rediss:// prefix (double-s) enables TLS. ElastiCache Serverless
    # requires TLS and rejects plaintext connections.
    value = local.redis_url
  }

  # ----- AWS / S3 ------------------------------------------------------------
  setting {
    namespace = "aws:elasticbeanstalk:application:environment"
    name      = "AWS_BUCKET_NAME"
    value     = aws_s3_bucket.application_data.id
  }
  setting {
    namespace = "aws:elasticbeanstalk:application:environment"
    name      = "AWS_REGION"
    value     = var.aws_region
  }

  # ----- AI provider keys ----------------------------------------------------
  setting {
    namespace = "aws:elasticbeanstalk:application:environment"
    name      = "OPENAI_API_KEY"
    value     = var.openai_api_key
  }
  setting {
    namespace = "aws:elasticbeanstalk:application:environment"
    name      = "ANTHROPIC_API_KEY"
    value     = var.anthropic_api_key
  }

  # ----- Admin panel ---------------------------------------------------------
  # DJANGO_SUPERUSER_* env vars trigger superuser creation on first deploy.
  # api/entrypoint.sh calls `manage.py createsuperuser --noinput` when these
  # are present. Idempotent — subsequent deploys leave the existing user alone.
  setting {
    namespace = "aws:elasticbeanstalk:application:environment"
    name      = "DJANGO_SUPERUSER_USERNAME"
    value     = "admin"
  }
  setting {
    namespace = "aws:elasticbeanstalk:application:environment"
    name      = "DJANGO_SUPERUSER_EMAIL"
    value     = "admin@example.com"
  }
  setting {
    namespace = "aws:elasticbeanstalk:application:environment"
    name      = "DJANGO_SUPERUSER_PASSWORD"
    value     = var.admin_panel_password
  }
}
