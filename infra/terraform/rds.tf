# -----------------------------------------------------------------------------
# rds.tf — the MySQL relational database that stores all chat data
#
# I run MySQL 8.4 (the current Long Term Support release) on RDS so I get
# automated backups, point-in-time recovery, and OS patching without managing
# a database server myself. Live production runs MySQL 8.0; staying on MySQL
# lets scripts/migration/dbcopy.py copy it (the tool refuses MySQL to MariaDB)
# and moving to 8.4 ends the paid 8.0 Extended Support.
#
# Key decisions:
#   - engine_version "8.4" names the major only, with automatic minor
#     upgrades: AWS applies 8.4.x patches and Terraform sees no drift. A pinned
#     8.4.x would show a downgrade diff after every automatic upgrade.
#   - Placed in private subnets — nothing outside the VPC can reach it.
#   - Encrypted at rest with AES-256 — required for any database holding
#     conversation transcripts and participant data.
#   - skip_final_snapshot = true on the test stack so it can be torn down
#     freely. On production I take a final snapshot first.
#   - deletion_protection (var.deletion_protection, true unless the test
#     stack turns it off) must be disabled before a destroy, which prevents
#     accidental data loss.
#   - TLS is available but not required (require_secure_transport off): the
#     app's DATABASES setting passes no SSL options, and the database is
#     reachable only from the application's security group.
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Parameter group — utf8mb4 so the database can store emoji and any Unicode
# character participants might type, with MySQL's default collation
# utf8mb4_0900_ai_ci. That is what live production's database uses (it runs
# the default parameter group), so the copied tables, the database default and
# tables the app creates after cutover all agree. Both values are MySQL 8.4's
# defaults; they are written out so a change is deliberate.
# -----------------------------------------------------------------------------

resource "aws_db_parameter_group" "main" {
  name        = "${local.name_prefix}-mysql84"
  description = "I configure MySQL 8.4 with full UTF-8 support (utf8mb4) so participant messages containing emoji or non-Latin characters are stored correctly."
  family      = "mysql8.4"

  parameter {
    name  = "character_set_server"
    value = "utf8mb4"
  }

  parameter {
    name  = "collation_server"
    value = "utf8mb4_0900_ai_ci"
  }

  tags = {
    Name = "${local.name_prefix}-mysql84-params"
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
  engine                     = "mysql"
  engine_version             = "8.4"
  auto_minor_version_upgrade = true
  instance_class             = var.db_instance_class

  # Storage — I start at 20 GB and allow auto-scaling up to 100 GB so the
  # database never runs out of space without manual intervention.
  allocated_storage     = 20
  max_allocated_storage = 100
  storage_type          = "gp3"
  storage_encrypted     = true

  # Database and credentials. Not live production's ebdb / ebroot: dbcopy
  # restores into whatever MIG_TARGET_DATABASE names (its tests copy between
  # differently named databases), and the app reads the name from
  # DATABASE_NAME, which elasticbeanstalk.tf already sets to chatbot_db.
  # Keeping these means no change to the app's config or the runbook.
  db_name  = "chatbot_db"
  username = "chatbot_user"

  # RDS generates the master password and keeps it in Secrets Manager
  # (rds!db-<id>, rotated every 7 days), so it never passes through
  # Terraform or its state. The application reads it at start
  # (DATABASE_SECRET_ID in elasticbeanstalk.tf) and again after a rotation
  # (api/generic_chatbot/db/base.py).
  manage_master_user_password = true

  # Networking — private subnets only, no public endpoint
  db_subnet_group_name   = aws_db_subnet_group.database.name
  vpc_security_group_ids = [aws_security_group.database.id]
  publicly_accessible    = false
  parameter_group_name   = aws_db_parameter_group.main.name

  # Backups
  # At least 7 days on production (variables.tf refuses fewer), so I can
  # restore to any point in the past week. The test stack keeps 1 day.
  backup_retention_period = var.backup_retention_period
  backup_window           = "03:00-04:00"
  maintenance_window      = "Mon:04:00-Mon:05:00"

  # Deletion behaviour
  # On the test stack: skip the final snapshot so I can destroy and recreate freely.
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
  deletion_protection       = var.deletion_protection

  tags = {
    Name = "${local.name_prefix}-database"
  }
}
