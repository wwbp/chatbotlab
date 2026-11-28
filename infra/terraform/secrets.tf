# -----------------------------------------------------------------------------
# secrets.tf — the application's secrets, looked up by name
#
# One Secrets Manager secret, <project>-<environment>/app, holds a JSON object:
#   SECRET_KEY            Django's signing key (sessions, CSRF, reset links)
#   OPENAI_API_KEY        optional
#   ANTHROPIC_API_KEY     optional
#   SURVEY_INGEST_TOKEN   optional; unset, /api/survey_response/ refuses all
#
# An administrator creates and fills it once, out of band (technical
# reference, "Secrets"). Terraform only looks it up: the apply role may
# describe secrets under <project>-<environment>/ but is denied every call
# that writes, restores, deletes or reads a value
# (bootstrap/iam/apply-*-guardrails.json, DenySecretValueCalls), since
# CreateSecret and UpdateSecret take a value. So no value enters Terraform
# state, the plan, the deploy workflow or the Elastic Beanstalk
# configuration, and no workflow can replace SECRET_KEY. The instance role
# may only read it (iam.tf).
#
# A plan fails until the secret exists; the application stops at start until
# it has a value. The secret is not destroyed with the stack, so the test
# stack keeps its own keys between rehearsals.
#
# The database password is not here: RDS keeps it (rds.tf).
# -----------------------------------------------------------------------------

data "aws_secretsmanager_secret" "app" {
  name = "${local.name_prefix}/app"
}
