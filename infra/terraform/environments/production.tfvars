# Non-secret settings for the production stack.
#
# The Deploy Infrastructure workflow reads this file. A change here is
# reviewed in a pull request and applied only by a manual run from main.
#
# Not here, on purpose:
#   environment, project_name   set by the workflow together with the state
#                               key; setting them here could cross the
#                               environments
#   passwords, API keys, tokens GitHub secrets (see the workflow header)

eb_instance_type  = "t3.small"
eb_max_instances  = 3
db_instance_class = "db.t3.micro"

# Production holds study data. These are also the defaults, written out so a
# change to them shows in review; variables.tf refuses any relaxation here.
deletion_protection     = true
backup_retention_period = 7
force_destroy_buckets   = false

# The web ACL in waf.tf (live production's rules). variables.tf refuses false.
waf_enabled = true

# Custom domain, e.g. "chatbot.mylab.org", a name in the Route 53 zone
# (dns_zone_name). Empty: the CloudFront URL is used. Setting it makes the
# next apply create and validate the certificate; the name moves to this
# stack only with cutover_dns = true (docs/deployment/data-migration.rst,
# step 11: the old distribution must give up the alias first).
domain_name = ""
cutover_dns = false

# Where invite and password-reset links point, and an allowed CORS/CSRF origin.
# Set apart from domain_name: moving the bot.wwbp.org alias to this stack's
# CloudFront is a cutover step, and the alias cannot be on two distributions.
site_url = "https://bot.wwbp.org"
