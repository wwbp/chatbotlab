# -----------------------------------------------------------------------------
# vpc.tf — the private network that contains all application infrastructure
#
# I create a single VPC with four subnets split across two availability zones:
#
#   Public subnets  (10.0.1.0/24, 10.0.2.0/24)
#     The Elastic Beanstalk load balancer and application instances live here.
#     They need outbound internet access to pull Docker images and reach the
#     OpenAI / Anthropic APIs.
#
#   Private subnets (10.0.10.0/24, 10.0.11.0/24)
#     The RDS database and ElastiCache cache live here. They have no route to
#     the internet — the only thing that can reach them is the application,
#     enforced by security group rules in security_groups.tf.
#
# Using two AZs means the load balancer keeps working if one AWS data centre
# has an outage. The database runs in a single AZ (cheaper) but
# the subnet group covers both AZs so I can enable Multi-AZ later with no
# infrastructure changes.
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# VPC
# -----------------------------------------------------------------------------

resource "aws_vpc" "main" {
  cidr_block = "10.0.0.0/16"

  # DNS hostnames are required so RDS and ElastiCache endpoints resolve
  # inside the VPC by their friendly DNS names rather than raw IP addresses.
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name = "${local.name_prefix}-vpc"
  }
}

# -----------------------------------------------------------------------------
# Internet Gateway — gives public subnets a route to the internet
# -----------------------------------------------------------------------------

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${local.name_prefix}-igw"
  }
}

# -----------------------------------------------------------------------------
# Public subnets — one per availability zone
# I assign public IPs to instances here so they can reach the internet
# without paying for a NAT Gateway (~$32/month per AZ).
# -----------------------------------------------------------------------------

resource "aws_subnet" "public" {
  count = 2

  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.${count.index + 1}.0/24"
  availability_zone       = local.availability_zones[count.index]
  map_public_ip_on_launch = true

  tags = {
    Name = "${local.name_prefix}-public-subnet-${count.index + 1}"
    Tier = "public"
  }
}

# -----------------------------------------------------------------------------
# Private subnets — one per availability zone
# No internet route. RDS and ElastiCache live here.
# -----------------------------------------------------------------------------

resource "aws_subnet" "private" {
  count = 2

  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.0.${count.index + 10}.0/24"
  availability_zone = local.availability_zones[count.index]

  tags = {
    Name = "${local.name_prefix}-private-subnet-${count.index + 1}"
    Tier = "private"
  }
}

# -----------------------------------------------------------------------------
# Route table for public subnets
# I add a default route (0.0.0.0/0) through the internet gateway so traffic
# from the application instances can reach the internet.
# -----------------------------------------------------------------------------

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name = "${local.name_prefix}-public-route-table"
  }
}

resource "aws_route_table_association" "public" {
  count = 2

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# -----------------------------------------------------------------------------
# Subnet groups — RDS and ElastiCache need to know which subnets they can
# place their endpoints in. I point both at the private subnets.
# -----------------------------------------------------------------------------

resource "aws_db_subnet_group" "database" {
  name        = "${local.name_prefix}-database-subnet-group"
  description = "I use these private subnets for the RDS MariaDB instance. Covering two AZs lets me enable Multi-AZ replication later without recreating this group."
  subnet_ids  = aws_subnet.private[*].id

  tags = {
    Name = "${local.name_prefix}-database-subnet-group"
  }
}

# Note: ElastiCache Serverless does not use a subnet group resource —
# I pass subnet IDs directly on the aws_elasticache_serverless_cache resource.
