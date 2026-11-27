#!/bin/bash
# Runs once, on first initialisation of the MySQL data volume.
#
# Django's test runner creates a separate `test_<DATABASE_NAME>` database, but
# the entrypoint only grants MYSQL_USER rights on MYSQL_DATABASE itself. Without
# the grant below every test errors at setup with:
#
#   (1044, "Access denied for user '<MYSQL_USER>'@'%' to database 'test_chatbot_db'")
#
# Uses $MYSQL_USER rather than a hardcoded name so it follows api/.env. A .sh
# init script is required for that — the entrypoint does no variable
# substitution in plain .sql files.
set -euo pipefail

mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" <<-EOSQL
	GRANT ALL PRIVILEGES ON \`test\_%\`.* TO '${MYSQL_USER}'@'%';
	FLUSH PRIVILEGES;
EOSQL
