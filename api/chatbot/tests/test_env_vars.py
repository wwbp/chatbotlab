"""
api/env_vars.txt lists every environment variable the deployed application
reads. infra/terraform/tests/env_parity.tftest.hcl checks that production's
Terraform sets each of them, or says why not. This test keeps the list in step
with the code: a new os.getenv() fails here until the name is listed (and so
reaches the Terraform check), and a name the code stops reading must be
removed.
"""

import re
from pathlib import Path

API = Path(__file__).resolve().parents[2]

# Python: os.getenv("X"), os.environ.get("X"), os.environ["X"],
# os.environ.setdefault("X", ...) and settings.py's _csv_env("X", ...).
PY_READ = re.compile(
    r"""(?:getenv|environ\.get|environ\.setdefault|_csv_env)\(\s*["']([A-Z][A-Z0-9_]*)["']"""
    r"""|environ\[\s*["']([A-Z][A-Z0-9_]*)["']"""
)
SH_READ = re.compile(r"\$\{?([A-Z][A-Z0-9_]*)")

# Developer tools that never run on the servers.
NOT_DEPLOYED = {"bench_latency.py", "locustfile.py", "smoke_survey_context.py"}


def names_read_by_code():
    names = set()
    for path in API.rglob("*.py"):
        rel = path.relative_to(API)
        if rel.name in NOT_DEPLOYED or "tests" in rel.parts or rel.parts[0] == ".venv":
            continue
        for match in PY_READ.finditer(path.read_text()):
            names.add(match.group(1) or match.group(2))
    for script in ("entrypoint.sh", "wait-for-db.sh"):
        names |= set(SH_READ.findall((API / script).read_text()))
    # The Dockerfile's other lines run at build time; only CMD runs on the server.
    for line in (API / "Dockerfile").read_text().splitlines():
        if line.startswith("CMD"):
            names |= set(SH_READ.findall(line))
    return names


def test_env_var_list_matches_the_code():
    # The name starts each line; comments and reasons are not names.
    text = (API / "env_vars.txt").read_text()
    listed = set(re.findall(r"^[A-Z][A-Z0-9_]*", text, re.MULTILINE))
    found = names_read_by_code()
    assert sorted(found - listed) == [], (
        "read by the code but missing from api/env_vars.txt"
    )
    assert sorted(listed - found) == [], (
        "in api/env_vars.txt but no longer read by the code"
    )


def test_scanner_finds_known_reads():
    # Guards the regexes: one name from each kind of read.
    found = names_read_by_code()
    assert {"SECRET_KEY", "CORS_ALLOWED_ORIGINS", "DJANGO_SETTINGS_MODULE"} <= found
    assert {"DJANGO_SUPERUSER_PASSWORD", "DATABASE_HOST", "GUNICORN_WORKERS"} <= found
    assert "LOCUST_TARGET_RPS" not in found
