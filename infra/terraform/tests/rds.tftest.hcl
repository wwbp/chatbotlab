# Runs offline: `terraform init -backend=false && terraform test`.
# The database matches live production's engine (MySQL), one major version up,
# so scripts/migration/dbcopy.py can copy it: the tool refuses MySQL to MariaDB.

mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{}"
    }
  }
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
    }
  }
}

mock_provider "aws" {
  alias = "us_east_1"
}

mock_provider "random" {}

variables {
  project_name         = "chatbotlab"
  environment          = "production"
  db_password          = "test-password-not-real"
  admin_panel_password = "test-password-not-real"
}

run "database_is_mysql_8_4" {
  command = plan

  assert {
    condition     = aws_db_instance.main.engine == "mysql" && aws_db_instance.main.engine_version == "8.4"
    error_message = "The database must be MySQL 8.4 (live production is MySQL 8.0; dbcopy refuses cross-engine copies)."
  }

  # engine_version names the major only, so AWS's minor upgrades are not drift.
  assert {
    condition     = aws_db_instance.main.auto_minor_version_upgrade
    error_message = "Minor versions must upgrade automatically (security patches within 8.4)."
  }

  assert {
    condition     = aws_db_parameter_group.main.family == "mysql8.4" && aws_db_instance.main.parameter_group_name == aws_db_parameter_group.main.name
    error_message = "The instance must use a mysql8.4 parameter group."
  }
}

# MySQL's own default collation, which live production's database uses: a new
# database created with it needs no ALTER DATABASE during restore, and tables
# the app creates after cutover get the same collation as the copied ones.
run "character_set_matches_live" {
  command = plan

  assert {
    condition = alltrue([
      for p in [
        { name = "character_set_server", value = "utf8mb4" },
        { name = "collation_server", value = "utf8mb4_0900_ai_ci" },
      ] : contains([for q in aws_db_parameter_group.main.parameter : { name = q.name, value = q.value }], p)
    ])
    error_message = "The parameter group must set utf8mb4 / utf8mb4_0900_ai_ci."
  }
}

run "storage_is_encrypted_gp3" {
  command = plan

  assert {
    condition     = aws_db_instance.main.storage_encrypted && aws_db_instance.main.storage_type == "gp3" && aws_db_instance.main.allocated_storage == 20
    error_message = "The database must be 20 GB of encrypted gp3."
  }

  assert {
    condition     = aws_db_instance.main.max_allocated_storage == 100
    error_message = "Storage autoscaling must stay on, up to 100 GB."
  }

  assert {
    condition     = !aws_db_instance.main.publicly_accessible
    error_message = "The database must not be publicly accessible (live production's is; the new stack must not be)."
  }
}
