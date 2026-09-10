# -----------------------------------------------------------------------------
# rds.tf — the MariaDB relational database that stores all chat data
#
# I run MariaDB 10.11 (the current Long Term Support release) on RDS so I get
# automated backups, point-in-time recovery, and OS patching without managing
# a database server myself.
#
# Key decisions:
#   - Placed in private subnets — nothing outside the VPC can reach it.
#   - Encrypted at rest with AES-256 — required for any database holding
#     conversation transcripts and participant data.
#   - skip_final_snapshot = true on staging so I can tear it down freely
#     during development. On production I take a final snapshot first.
#   - deletion_protection = true on production means I must manually disable
#     it before a destroy, which prevents accidental data loss.
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Parameter group — I set UTF-8 MB4 so the database can store emoji and
# any Unicode character participants might type.
# -----------------------------------------------------------------------------

resource "aws_db_parameter_group" "mariadb" {
  name        = "${local.name_prefix}-mariadb"
  description = "I configure MariaDB 10.11 with full UTF-8 support (utf8mb4) so participant messages containing emoji or non-Latin characters are stored correctly."
  family      = "mariadb10.11"

  parameter {
    name  = "character_set_server"
    value = "utf8mb4"
  }

  parameter {
    name  = "collation_server"
    value = "utf8mb4_unicode_ci"
  }

  tags = {
    Name = "${local.name_prefix}-mariadb-params"
  }
}

# -----------------------------------------------------------------------------
# RDS instance
# -----------------------------------------------------------------------------

# Suffix for the final snapshot name, so a redeployed stack does not collide
# with the snapshot left behind by the previous one. Kept in state, so it is
# stable for the life of a deployment: re-applying an existing stack never
# renames its pending snapshot, while a stack stood up after a teardown gets a
# fresh value. keepers ties it to the deployment identity for the same reason.
resource "random_id" "final_snapshot_suffix" {
  byte_length = 4

  keepers = {
    name_prefix = local.name_prefix
  }
}

resource "aws_db_instance" "main" {
  identifier = "${local.name_prefix}-database"

  # Engine
  engine         = "mariadb"
  engine_version = "10.11"
  instance_class = var.db_instance_class

  # Storage — I start at 20 GB and allow auto-scaling up to 100 GB so the
  # database never runs out of space without manual intervention.
  allocated_storage     = 20
  max_allocated_storage = 100
  storage_type          = "gp3"
  storage_encrypted     = true

  # Database and credentials
  db_name  = "chatbot_db"
  username = "chatbot_user"
  password = var.db_password

  # Networking — private subnets only, no public endpoint
  db_subnet_group_name   = aws_db_subnet_group.database.name
  vpc_security_group_ids = [aws_security_group.database.id]
  publicly_accessible    = false
  parameter_group_name   = aws_db_parameter_group.mariadb.name

  # Backups
  # I keep 7 days of backups on production so I can restore to any point in
  # the past week. Staging only keeps 1 day — enough to recover from a
  # mistake during development without paying for extra storage.
  backup_retention_period = var.environment == "production" ? 7 : 1
  backup_window           = "03:00-04:00"
  maintenance_window      = "Mon:04:00-Mon:05:00"

  # Deletion behaviour
  # On staging: skip the final snapshot so I can destroy and recreate freely.
  # On production: take a final snapshot and block deletion until I explicitly
  # disable deletion_protection — this prevents accidental data loss.
  #
  # The snapshot name carries a per-deployment suffix. A fixed name cannot be
  # reused: the snapshot outlives the database it came from, so the *second*
  # teardown of a redeployed stack fails with
  #   DBSnapshotAlreadyExists: Cannot create the snapshot because a snapshot
  #   with the identifier <prefix>-final-snapshot already exists.
  # leaving the database running and the destroy half-finished. The suffix is
  # held in state, so it is stable for the life of a deployment and only
  # changes when a new one is stood up — which is exactly when a new name is
  # needed.
  skip_final_snapshot       = var.environment != "production"
  final_snapshot_identifier = var.environment == "production" ? "${local.name_prefix}-final-snapshot-${random_id.final_snapshot_suffix.hex}" : null
  deletion_protection       = var.environment == "production"

  tags = {
    Name = "${local.name_prefix}-database"
  }
}
