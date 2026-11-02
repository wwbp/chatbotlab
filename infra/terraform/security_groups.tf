# -----------------------------------------------------------------------------
# security_groups.tf — firewall rules for every layer of the stack
#
# I follow a strict least-privilege chain: each layer only accepts traffic
# from the layer directly in front of it.
#
#   Internet (0.0.0.0/0)
#       │  port 80, 443
#       ▼
#   load_balancer (ALB)
#       │  port 80
#       ▼
#   app_instances (EB EC2)
#       │  port 3306          port 6379
#       ▼                         ▼
#   database (RDS)            cache (ElastiCache)
#
# Nothing can reach RDS or ElastiCache from outside the VPC — not even from
# the internet-facing public subnets — because the security groups only
# allow traffic originating from the app_instances security group.
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Load balancer security group
# The Elastic Beanstalk ALB uses this group. I assign it explicitly in
# elasticbeanstalk.tf so I can reference it in the app instances rule below.
# -----------------------------------------------------------------------------

# AWS's list of CloudFront's origin-facing addresses (same name in every region).
data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

resource "aws_security_group" "load_balancer" {
  name        = "${local.name_prefix}-load-balancer"
  description = "I allow only CloudFront into the Elastic Beanstalk load balancer. CloudFront is the only entry point to the application from the internet."
  vpc_id      = aws_vpc.main.id

  # Only CloudFront, so nobody can skip it and forge the client IP the access
  # log records (api/chatbot/access_log.py). Port 80 only: CloudFront reaches
  # this origin http-only (cloudfront.tf) and the ALB has no HTTPS listener.
  # The prefix list counts as 55 of a security group's 60 rules, so one rule.
  ingress {
    description     = "HTTP from CloudFront only"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront.id]
  }

  egress {
    description = "I allow all outbound traffic so the ALB can forward requests to application instances on any port"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.name_prefix}-load-balancer-sg"
  }
}

# -----------------------------------------------------------------------------
# Application instances security group
# Elastic Beanstalk EC2 instances use this group. I only allow port 80 from
# the load balancer — direct internet access to instances is blocked even
# though instances have public IPs (the SG acts as the firewall).
# -----------------------------------------------------------------------------

resource "aws_security_group" "app_instances" {
  name        = "${local.name_prefix}-app-instances"
  description = "I allow the load balancer to send HTTP traffic to application instances, and allow the instances to reach the internet, the database, and the cache."
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "HTTP from the load balancer only - not from the open internet"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    security_groups = [aws_security_group.load_balancer.id]
  }

  egress {
    description = "I allow all outbound traffic so instances can pull Docker images, reach OpenAI/Anthropic APIs, and connect to RDS and ElastiCache"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.name_prefix}-app-instances-sg"
  }
}

# -----------------------------------------------------------------------------
# Database security group
# Only the application instances can connect to RDS on the MariaDB port.
# -----------------------------------------------------------------------------

resource "aws_security_group" "database" {
  name        = "${local.name_prefix}-database"
  description = "I allow MariaDB connections only from the application instances. Nothing else - not the load balancer, not the internet - can reach the database."
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "MariaDB port from application instances only"
    from_port       = 3306
    to_port         = 3306
    protocol        = "tcp"
    security_groups = [aws_security_group.app_instances.id]
  }

  egress {
    description = "I allow outbound traffic so RDS can reach AWS internal services for backups and monitoring"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.name_prefix}-database-sg"
  }
}

# -----------------------------------------------------------------------------
# Cache security group
# Only the application instances can connect to ElastiCache on the Redis port.
# ElastiCache Serverless always uses TLS, so connections go over port 6379
# encrypted — no plaintext Redis traffic.
# -----------------------------------------------------------------------------

resource "aws_security_group" "cache" {
  name        = "${local.name_prefix}-cache"
  description = "I allow Redis TLS connections only from the application instances. ElastiCache Serverless enforces TLS on all connections automatically."
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Redis TLS port from application instances only"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.app_instances.id]
  }

  egress {
    description = "I allow outbound traffic so ElastiCache can reach AWS internal services"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.name_prefix}-cache-sg"
  }
}
