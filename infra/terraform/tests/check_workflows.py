#!/usr/bin/env python3
"""Guard checks on the infrastructure workflows, as plain text.

Run by the "Terraform test" CI job: python3 infra/terraform/tests/check_workflows.py
Standard library only. Each check exists because the hazard it names was once
in the workflow; a later edit must not bring it back.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"
failures = []


def check(ok, message):
    if not ok:
        failures.append(message)


def uncommented(path):
    """File text without comment-only lines, so prose may name a hazard."""
    lines = path.read_text().splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def block(text, header):
    """The line matching `header` plus every following line indented deeper
    (blank lines included): a YAML step or job, or a shell if/case body."""
    match = re.search(rf"^( *)(?:- )?{header}.*$", text, re.M)
    if not match:
        return ""
    indent = len(match.group(1))
    lines = [match.group(0)]
    for line in text[match.end() + 1 :].splitlines():
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        lines.append(line)
    return "\n".join(lines) + "\n"


deploy = uncommented(WORKFLOWS / "deploy-infrastructure.yml")

# A failed or repeated apply must leave every resource in place. The workflow
# used to run `terraform destroy` on any failure, and to delete the database
# with no snapshot when it judged the state "orphaned".
for pattern in (
    r"terraform\s+destroy",
    r"terraform\s+state\s+rm",
    r"terraform\s+import",
    r"terraform\s+apply[^\n]*-destroy",
    r"\baws\s+\S+\s+(delete|terminate|remove)-",
    r"\baws\s+s3\s+(rm|rb)\b",
):
    found = re.search(pattern, deploy)
    check(
        not found,
        f"deploy-infrastructure.yml must never destroy or delete: found "
        f"'{found.group(0) if found else ''}'",
    )

# --- State: one per environment, locked, in one predictably named bucket ----

TERRAFORM = ROOT / "infra" / "terraform"
destroy = uncommented(WORKFLOWS / "destroy-infrastructure.yml")
infra_workflows = {"deploy-infrastructure.yml": deploy, "destroy-infrastructure.yml": destroy}
all_workflows = {p.name: uncommented(p) for p in sorted(WORKFLOWS.glob("*.yml"))}

for name, text in infra_workflows.items():
    # The environment comes from the person who started the run. A literal
    # here once made every run build "production", whatever was intended.
    values = re.findall(r"TF_VAR_environment:\s*(.+)", text)
    check(values, f"{name}: TF_VAR_environment is not set")
    for value in values:
        check(
            "inputs.environment" in value or "matrix.environment" in value,
            f"{name}: TF_VAR_environment must come from inputs.environment (or the "
            f"plan job's matrix), got '{value.strip()}'",
        )
    check(
        re.search(r"\n      environment:\n(?:        .*\n)*?        required: true", text),
        f"{name}: needs a required 'environment' input",
    )
    check(
        not re.search(r"\n      environment:\n(?:        .*\n)*?        default:", text),
        f"{name}: the 'environment' input must have no default",
    )
    # Two environments in one state file would make each apply delete the other.
    keys = re.findall(r'backend-config="key=([^"]*)"', text)
    check(keys, f"{name}: no state key passed to terraform init")
    for key in keys:
        check(
            "ENVIRONMENT" in key.upper(),
            f"{name}: state key '{key}' does not contain the environment",
        )
    # One naming scheme for the state bucket, so every tool finds the same one.
    for bucket in re.findall(r'BUCKET="([^"]*tfstate[^"]*)"', text):
        check(
            bucket == "${TF_PROJECT_PREFIX}-tfstate-${ACCOUNT_ID}",
            f"{name}: state bucket must be <prefix>-tfstate-<account>, got '{bucket}'",
        )

# Emptying buckets by project prefix alone would empty the other environment's.
check(
    "starts_with(Name, '${PREFIX}-${ENVIRONMENT}-')" in destroy,
    "destroy-infrastructure.yml: bucket emptying must be limited to <prefix>-<environment>-",
)
check(
    "-production-" not in destroy and "-staging-" not in destroy,
    "destroy-infrastructure.yml: resource names must not hardcode an environment",
)

main_tf = uncommented(TERRAFORM / "main.tf")
bootstrap_tf = uncommented(TERRAFORM / "bootstrap" / "main.tf")
setup_sh = uncommented(TERRAFORM / "setup.sh")

# Locking lives in the backend block, so every `terraform init` gets it and no
# caller can forget the flag.
check(
    re.search(r'backend "s3" \{[^}]*use_lockfile\s*=\s*true', main_tf),
    "main.tf: the s3 backend must set use_lockfile = true",
)
for name, text in {**all_workflows, "setup.sh": setup_sh}.items():
    check("use_lockfile=false" not in text, f"{name}: must not switch the state lock off")
    # The one exception is the pull-request plan, checked further down.
    if name != "deploy-infrastructure.yml":
        check("-lock=false" not in text, f"{name}: must not run terraform without the state lock")

# use_lockfile needs Terraform 1.11. Every place that pins a version must agree.
pins = {
    name: re.findall(r"terraform_version:\s*'([^']+)'", text)
    for name, text in all_workflows.items()
}
flat = {pin for found in pins.values() for pin in found}
check(len(flat) == 1, f"workflows pin different Terraform versions: {sorted(flat)}")
for pin in flat:
    minor = tuple(int(n) for n in re.findall(r"\d+", pin)[:2])
    check(minor >= (1, 11), f"Terraform pin '{pin}' is older than 1.11 (no S3 state lock)")
for name, text in {"main.tf": main_tf, "bootstrap/main.tf": bootstrap_tf}.items():
    check(
        re.search(r'required_version\s*=\s*">= 1\.1[1-9]', text),
        f"{name}: required_version must be at least 1.11",
    )

check(
    "random_id" not in bootstrap_tf and "account_id}" in bootstrap_tf,
    "bootstrap/main.tf: state bucket must be <prefix>-tfstate-<account>, not a random suffix",
)
check(
    re.search(r'backend-config="key=[^"]*ENVIRONMENT[^"]*"', setup_sh),
    "setup.sh: state key must contain the environment",
)
for path in (TERRAFORM / "setup.sh", TERRAFORM / "README.md", TERRAFORM / "bootstrap" / "main.tf", TERRAFORM / "main.tf"):
    check(
        "dynamodb" not in path.read_text().lower() and "lock table" not in path.read_text().lower(),
        f"{path.name}: mentions a DynamoDB lock table that nothing creates",
    )


# --- Deploy secrets: only the chosen environment's, only when asked ----------

staging_yml = all_workflows["staging.yml"]
production_yml = all_workflows["production.yml"]


def aws_secrets(text):
    return set(re.findall(r"secrets\.(AWS_\w+)", text))


# The staging and production deploy workflows must not read one secret in
# common, or writing it for one environment repoints the other. production.yml
# may fall back to a shared name (`secrets.AWS_PROD_X || secrets.AWS_X`) only
# after its own name, so deployments set up before the split keep working.
check(
    not any("PROD" in name for name in aws_secrets(staging_yml)),
    "staging.yml reads a production secret",
)
# Only these three may fall back, each to its own old shared name. Any other
# fallback (e.g. the production EB environment or bucket to staging's) would
# deploy production onto the staging stack whenever the PROD secret is unset.
ALLOWED_FALLBACKS = {
    (f"AWS_PROD_{x}", f"AWS_{x}") for x in ("ACCOUNT_ID", "ROLE_NAME", "BACKEND_APPLICATION_NAME")
}
fallbacks = set()
for name, text in all_workflows.items():
    for m in re.finditer(r"\|\|\s*secrets\.(\w+)", text):
        before = re.search(r"secrets\.(\w+)\s*$", text[: m.start()])
        pair = (before.group(1) if before else "<not a secret>", m.group(1))
        check(
            name == "production.yml" and pair in ALLOWED_FALLBACKS,
            f"{name}: secret fallback '{pair[0]} || {pair[1]}' is not allowed; only "
            f"production.yml may use secrets.AWS_PROD_X || secrets.AWS_X for "
            f"X in ACCOUNT_ID, ROLE_NAME, BACKEND_APPLICATION_NAME",
        )
        fallbacks.add(pair)
without_fallbacks = production_yml
for prod, old in ALLOWED_FALLBACKS:
    without_fallbacks = re.sub(rf"(secrets\.{prod})\s*\|\|\s*secrets\.{old}\b", r"\1", without_fallbacks)
shared = {name for name in aws_secrets(without_fallbacks) if "PROD" not in name}
check(not shared, f"production.yml reads secrets that staging.yml owns: {sorted(shared)}")

# The job that writes the secrets must belong to the chosen GitHub Environment,
# so that environment's protection rules (reviewers, branches) apply to it,
# and must take ENVIRONMENT from the input and nowhere else.
secrets_job = next(
    (j for j in re.split(r"\n(?=  [\w-]+:\n)", "\n" + deploy.split("\njobs:\n", 1)[-1])
     if "name: Write deployment secrets" in j),
    "",
)
check(
    re.search(r"\n    environment: \$\{\{ inputs\.environment \}\}\n", secrets_job),
    "deploy-infrastructure.yml: the job that writes deploy secrets needs 'environment: ${{ inputs.environment }}'",
)
env_values = re.findall(r"\n\s+ENVIRONMENT:\s*(.+)", secrets_job)
check(
    env_values and all(v.strip() == "${{ inputs.environment }}" for v in env_values),
    f"deploy-infrastructure.yml: ENVIRONMENT must come from inputs.environment, got {env_values}",
)

# A missing secret must stop the run before it touches AWS.
check(
    re.search(r"\n {10}(if \[\[ \$fail -eq 1 \]\]; then exit 1; fi|\[\[ \$fail -eq 1 \]\] && exit 1)\n",
              block(deploy, r"name: Check required secrets")),
    "deploy-infrastructure.yml: 'Check required secrets' must exit 1 when a required secret is missing",
)

# While production.yml still falls back to the shared names, writing staging's
# secrets would repoint production. The preflight must stop that run.
preflight = block(block(deploy, r"name: Check required secrets"), r'if \[\[ "\$ENVIRONMENT" == "staging" \]\]; then')
check(preflight, "deploy-infrastructure.yml: no staging preflight ('if [[ \"$ENVIRONMENT\" == \"staging\" ]]')")
loop = re.search(r"for name in ([\w ]+); do", preflight)
checked = set(loop.group(1).split()) if loop else set()
check(
    checked == {prod for prod, _ in fallbacks} and checked,
    f"deploy-infrastructure.yml: the staging preflight must check exactly the AWS_PROD_* "
    f"names production.yml falls back from; checks {sorted(checked)}",
)
check(
    'if ! grep -qx "$name"' in preflight and "fail=1" in preflight
    and re.search(r"if \[\[ \$fail -eq 1 \]\]; then exit 1; fi", preflight),
    "deploy-infrastructure.yml: the staging preflight must fail (exit 1) when an AWS_PROD_* secret is missing",
)

check(
    re.search(r"\n      write_deploy_secrets:\n(?:        .*\n)*?        default: false", deploy),
    "deploy-infrastructure.yml: needs a 'write_deploy_secrets' input that defaults to false",
)
check(
    len(re.findall(r"gh secret set (?!__PREFLIGHT_TEST__)", deploy)) == 1 and "set_secret()" in deploy,
    "deploy-infrastructure.yml: 'gh secret set' may appear once, inside set_secret() "
    "(plus the token preflight)",
)
step = re.search(r"- name: Write deployment secrets\n(?:        .*\n|\n)+", deploy)
check(step, "deploy-infrastructure.yml: 'Write deployment secrets' step not found")
step = step.group(0) if step else ""
check(
    re.search(
        r"\n        if: (\$\{\{ *)?(inputs\.action == 'apply' && )?inputs\.write_deploy_secrets( *\}\})? *\n",
        step,
    ),
    "deploy-infrastructure.yml: the secrets step's if: must be exactly "
    "[inputs.action == 'apply' && ]inputs.write_deploy_secrets",
)
check(
    len(re.findall(r"\bset_secret\s+AWS_", deploy)) == len(re.findall(r"\bset_secret\s+AWS_", step)),
    "deploy-infrastructure.yml: set_secret is called outside the 'Write deployment secrets' step",
)
written = {}
for env_name in ("staging", "production"):
    branch = re.search(rf"\n\s+{env_name}\)\n(.*?)\n\s+;;", step, re.S)
    check(branch, f"deploy-infrastructure.yml: no '{env_name})' branch in the secrets step")
    written[env_name] = set(re.findall(r"set_secret\s+(\w+)", branch.group(1))) if branch else set()
check(
    len(re.findall(r"\bset_secret\s+AWS_", step)) == sum(len(names) for names in written.values()),
    "deploy-infrastructure.yml: a secret is written outside the staging/production branches",
)
check(
    written["staging"] == aws_secrets(staging_yml),
    f"building staging must write exactly the secrets staging.yml reads; "
    f"difference: {sorted(written['staging'] ^ aws_secrets(staging_yml))}",
)
production_reads = {name for name in aws_secrets(production_yml) if "PROD" in name}
check(
    written["production"] == production_reads,
    f"building production must write exactly the AWS_*PROD* secrets production.yml reads; "
    f"difference: {sorted(written['production'] ^ production_reads)}",
)

# setup.sh tells a person which secret to set to which output. It must give the
# same (secret, output) pairs as the workflow, and only the built environment's:
# one list for both once led to staging's bucket secret naming production's.
def pairs(text, call):
    found = {}
    for env_name in ("staging", "production"):
        body = block(text, rf"{env_name}\)")
        found[env_name] = set(re.findall(rf'\b{call}\s+(\w+)\s+"?\$?\(?(?:out\s+)?(\w+)', body))
    return found


setup_pairs = pairs(setup_sh, "show")
workflow_pairs = pairs(step, "set_secret")
for env_name in ("staging", "production"):
    check(
        setup_pairs[env_name] and setup_pairs[env_name] == workflow_pairs[env_name],
        f"setup.sh must print the same {env_name} (secret, output) pairs as the workflow writes; "
        f"difference: {sorted(setup_pairs[env_name] ^ workflow_pairs[env_name])}",
    )
check(
    len(re.findall(r"\bshow\s+AWS_", setup_sh)) == sum(len(p) for p in setup_pairs.values()),
    "setup.sh: a secret is printed outside the staging/production branches",
)
check(
    re.search(r'case "\$ENVIRONMENT" in', setup_sh),
    "setup.sh: must choose the secret names by $ENVIRONMENT",
)
# An output description that names a staging secret without saying so invites
# setting it from a production stack.
outputs_tf = (TERRAFORM / "outputs.tf").read_text()
for description in re.findall(r'description\s*=\s*"([^"]*)"', outputs_tf):
    for secret in aws_secrets(staging_yml):
        for m in re.finditer(rf"\b{secret}\b", description):
            check(
                description[m.end():].startswith(" for staging"),
                f"outputs.tf: description names staging's {secret} without ' for staging': '{description}'",
            )

# Building infrastructure must not also start an application deploy.
check(
    "gh workflow run" not in deploy,
    "deploy-infrastructure.yml: must not trigger another workflow",
)

# --- Plan on pull requests; apply only by hand, behind a GitHub Environment --


def jobs_of(text):
    """Job name -> job text, from a workflow's `jobs:` section."""
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"\n  ([\w-]+):\n", "\n" + body)
    return dict(zip(parts[1::2], parts[2::2]))


def steps_of(job):
    return re.split(r"\n      - ", job)[1:]


triggers = deploy.split("\njobs:\n", 1)[0]
check(
    not re.search(r"\n  (push|schedule|pull_request_target|workflow_run):", triggers),
    "deploy-infrastructure.yml: may be started only by workflow_dispatch and pull_request",
)
check(
    re.search(r"\n  pull_request:\n    paths:\n(?:      .*\n)*?      - 'infra/terraform/\*\*'", triggers),
    "deploy-infrastructure.yml: must plan on pull requests that touch infra/terraform/**",
)
check(
    "\npermissions: {}\n" in triggers,
    "deploy-infrastructure.yml: top-level permissions must be empty; each job asks for its own",
)
for name, text in infra_workflows.items():
    grants = set(re.findall(r"\n\s+([\w-]+): write\b", text))
    check(
        grants <= {"id-token"},
        f"{name}: the only write permission allowed is id-token (OIDC), found {sorted(grants)}",
    )
# Short-lived credentials from GitHub OIDC only; no stored AWS keys.
check(
    not re.search(r"aws-access-key-id|aws-secret-access-key|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY", deploy),
    "deploy-infrastructure.yml: must not use long-lived AWS access keys",
)

deploy_jobs = jobs_of(deploy)
check(set(deploy_jobs) == {"plan", "apply"}, f"deploy-infrastructure.yml: expected jobs plan and apply, found {sorted(deploy_jobs)}")
plan_job = deploy_jobs.get("plan", "")
apply_job = deploy_jobs.get("apply", "")

# The pull-request plan runs code from the pull request, so it gets a
# read-only role, no GitHub Environment and no real application secrets.
check(
    re.search(r"\n    if: github\.event_name == 'pull_request'\n", "\n" + plan_job),
    "plan job: must run only on pull_request",
)
check("\n    environment:" not in "\n" + plan_job, "plan job: must not use a GitHub Environment")
check(not re.search(r"terraform\s+apply", plan_job), "plan job: must never apply")
plan_secrets = set(re.findall(r"secrets\.(\w+)", plan_job))
allowed = {"TF_PLAN_ROLE_ARN", "TF_PROJECT_PREFIX", "CREATE_GITHUB_OIDC_PROVIDER"}
check(
    plan_secrets <= allowed,
    f"plan job: may read only {sorted(allowed)}, also reads {sorted(plan_secrets - allowed)}",
)
check("configured == 'true'" in plan_job, "plan job: must skip cleanly when its role is not configured")
for line in deploy.splitlines():
    if "-lock=false" in line:
        check(
            "terraform plan" in line and line in plan_job,
            f"deploy-infrastructure.yml: -lock=false is allowed only on the pull-request plan: '{line.strip()}'",
        )

# Applying is a deliberate act by a person, approved through the GitHub
# Environment of the same name as the stack.
for name, text in {"deploy-infrastructure.yml": deploy}.items():
    for job_name, job in jobs_of(text).items():
        if not re.search(r"terraform\s+(apply|destroy)", job):
            continue
        check(
            "\n    environment: ${{ inputs.environment }}\n" in "\n" + job,
            f"{name}: job '{job_name}' changes infrastructure and must declare "
            f"'environment: ${{{{ inputs.environment }}}}'",
        )
check(
    re.search(r"\n    if: github\.event_name == 'workflow_dispatch'\n", "\n" + apply_job),
    "apply job: must run only on workflow_dispatch",
)
check(
    re.search(r"\n    concurrency:", "\n" + apply_job),
    "apply job: needs a concurrency group so two runs cannot overlap",
)
check(
    re.search(r"\n      action:\n(?:        .*\n)*?        default: plan\n", triggers),
    "deploy-infrastructure.yml: needs an 'action' input that defaults to plan",
)
for step in steps_of(apply_job):
    if re.search(r"terraform\s+apply|gh secret set (?!__PREFLIGHT_TEST__)|set_secret ", step):
        title = step.splitlines()[0]
        check(
            re.search(r"\n        if: [^\n]*inputs\.action == 'apply'", step),
            f"apply job, step '{title}': changes something, so it must be conditional on inputs.action == 'apply'",
        )
# The full apply runs the plan that was just printed, nothing else.
full_applies = [
    line.strip() for line in apply_job.splitlines()
    if re.search(r"terraform\s+apply", line) and "-target" not in line
]
check(
    full_applies == ["terraform apply -input=false tfplan"],
    f"apply job: the full apply must be exactly 'terraform apply -input=false tfplan', found {full_applies}",
)

# Per-environment settings live in git, where the pull-request plan sees them.
for env_name in ("staging", "production"):
    path = TERRAFORM / "environments" / f"{env_name}.tfvars"
    check(path.exists(), f"environments/{env_name}.tfvars is missing")
    text = uncommented(path) if path.exists() else ""
    names = set(re.findall(r"^\s*(\w+)\s*=", text, re.M))
    check(
        not names & {"environment", "project_name"},
        f"environments/{env_name}.tfvars: must not set environment or project_name; "
        f"the workflow sets them, together with the state key",
    )
    secret_like = {n for n in names if re.search(r"password|secret|key|token", n)}
    check(not secret_like, f"environments/{env_name}.tfvars: secrets do not belong in git: {sorted(secret_like)}")
for line in deploy.splitlines():
    if re.search(r"terraform\s+plan", line):
        check(
            '-var-file="environments/${ENVIRONMENT}.tfvars"' in line,
            f"deploy-infrastructure.yml: every plan must read the environment's settings file: '{line.strip()}'",
        )

if failures:
    print("\n".join(f"FAIL: {f}" for f in failures))
    sys.exit(1)
print("workflow guard checks passed")
