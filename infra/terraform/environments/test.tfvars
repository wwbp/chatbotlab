# Non-secret settings for the on-demand test stack. It lives in the same AWS
# account as production (and the hand-built live deployment); the workflow
# tags everything Environment=test, and the test role may touch only that.
# Applied for a rehearsal or a risky change, tested, then destroyed with the
# Destroy Infrastructure workflow.
#
# email_backend stays unset: the test stack's email goes to the application
# log. It must not send through the account's SES identities (the test
# boundary denies ses:Send* as well).
#
# Not here, on purpose:
#   environment, project_name   set by the workflow together with the state
#                               key
#   passwords, API keys, tokens GitHub secrets (see the workflow header)

eb_instance_type  = "t3.small"
eb_max_instances  = 1
db_instance_class = "db.t3.micro"

# Disposable: no deletion protection, one day of backups, and terraform
# destroy may delete the buckets with their objects. Only the test stack may
# set these (variables.tf); it holds no study data.
deletion_protection     = false
backup_retention_period = 1
force_destroy_buckets   = true

domain_name = ""
