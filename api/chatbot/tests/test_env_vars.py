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


# Reads PY_READ cannot name: a non-literal name, `"X" in os.environ`, or
# environ/getenv imported under another name.
HIDDEN_READ = re.compile(
    r"""(?:getenv|environ\.get|environ\.setdefault)\(\s*(?!["'\s])"""
    r"""|environ\[\s*(?!["'])"""
    r"""|\bin\s+os\.environ\b"""
    r"""|from\s+os\s+import\b[^\n]*\b(?:environ|getenv)\b"""
)
# settings.py's _csv_env(name, default) reads its argument; PY_READ catches
# each _csv_env("X", ...) call instead.
ALLOWED_HIDDEN = {("generic_chatbot/settings.py", 'getenv(name, "")')}


def deployed_python():
    for path in API.rglob("*.py"):
        rel = path.relative_to(API)
        if rel.name in NOT_DEPLOYED or "tests" in rel.parts or rel.parts[0] == ".venv":
            continue
        yield rel.as_posix(), path.read_text()


def names_read_by_code():
    names = set()
    for _, text in deployed_python():
        for match in PY_READ.finditer(text):
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


def test_every_env_read_names_its_variable():
    # Otherwise a read hides from the list and from the Terraform check.
    # Whole files, not lines: calls often put the name on the next line.
    hidden = []
    for rel, text in deployed_python():
        for match in HIDDEN_READ.finditer(text):
            line = text[text.rfind("\n", 0, match.start()) + 1 :].split("\n", 1)[0]
            if not any(r == rel and a in line for r, a in ALLOWED_HIDDEN):
                hidden.append(f"{rel}: {line.strip()}")
    assert hidden == [], 'read env vars as os.getenv("NAME") so the scan sees them'


def test_hidden_read_pattern():
    for line in (
        "Y = os.getenv(NEW_KEY)",
        "os.environ.get(key)",
        "os.environ[key]",
        'if "X" in os.environ:',
        "from os import environ as e",
        "from os import getenv",
    ):
        assert HIDDEN_READ.search(line), line
    assert not HIDDEN_READ.search('os.getenv("X", "1")')
    assert not HIDDEN_READ.search('os.environ["X"]')
