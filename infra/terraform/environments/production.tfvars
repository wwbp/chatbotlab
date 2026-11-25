# Non-secret settings for the production stack.
#
# The Deploy Infrastructure workflow reads this file for both its jobs, so a
# change here is planned on the pull request and applied only by a manual run.
#
# Not here, on purpose:
#   environment, project_name   set by the workflow together with the state
#                               key; setting them here could cross the two
#                               environments
#   passwords, API keys, tokens GitHub secrets (see the workflow header)

eb_instance_type  = "t3.small"
eb_max_instances  = 1
db_instance_class = "db.t3.micro"

# Custom domain, e.g. "chatbot.mylab.org". Empty: the CloudFront URL is used.
# Setting it makes the next apply create a certificate and print the DNS
# record to add.
domain_name = ""
