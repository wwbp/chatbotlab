#!/usr/bin/env python3
"""Guard checks on the infrastructure workflows, as plain text.

Run by the "Terraform test" CI job: python3 infra/terraform/tests/check_workflows.py
Standard library only. Each check exists because the hazard it names was once
in the workflow; a later edit must not bring it back.
"""

import json
import re
import sys
from fnmatch import fnmatchcase
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"
failures = []


def check(ok, message):
    if not ok:
        failures.append(message)


# A terraform subcommand, allowing global options in between
# (`terraform -chdir=infra/terraform apply`).
TF = r"\bterraform\b[^\n|;&]*?\b"


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
    TF + r"destroy\b",
    TF + r"state\s+rm\b",
    TF + r"import\b",
    TF + r"apply\b[^\n]*-destroy",
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
    grants = set(re.findall(r"([\w-]+):\s*write\b", text)) | set(re.findall(r"\bwrite-all\b", text))
    check(
        grants <= {"id-token"},
        f"{name}: the only write permission allowed is id-token (OIDC), found {sorted(grants)}",
    )
# Short-lived credentials from GitHub OIDC only; no stored AWS keys.
for name, text in all_workflows.items():
    check(
        not re.search(r"aws-access-key-id|aws-secret-access-key|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY", text),
        f"{name}: must not use long-lived AWS access keys",
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
check(not re.search(TF + r"apply\b", plan_job), "plan job: must never apply")
plan_secrets = set(re.findall(r"secrets\.(\w+)", plan_job))
allowed = {"TF_PLAN_ROLE_ARN", "TF_PROJECT_PREFIX"}
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
for name, text in infra_workflows.items():
    for job_name, job in jobs_of(text).items():
        if not re.search(TF + r"(apply|destroy)\b", job):
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
# `inputs.action == 'apply'`, optionally narrowed with `&& …`; never widened.
APPLY_IF = (
    r"\n        if: (\$\{\{ *)?inputs\.action == 'apply'"
    r"( && (?:(?!\|\||always\(\)|cancelled\(\)|failure\(\))[^\n])+?)?( *\}\})? *\n"
)
for step in steps_of(apply_job):
    if re.search(TF + r"apply\b|gh secret set (?!__PREFLIGHT_TEST__)|set_secret ", step):
        title = step.splitlines()[0]
        check(
            re.search(APPLY_IF, step),
            f"apply job, step '{title}': changes something, so its if: must be inputs.action == 'apply' "
            f"(optionally '&& …', never '||', always() or !cancelled())",
        )
# The full apply runs the plan that was just printed, nothing else.
full_applies = [
    line.strip() for line in apply_job.splitlines()
    if re.search(TF + r"apply\b", line) and "-target" not in line
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

# --- Apply only what was planned, and only what was meant ---------------------

apply_steps = steps_of(apply_job)
titles = [st.splitlines()[0] for st in apply_steps]


def step_index(prefix):
    return next((i for i, t in enumerate(titles) if t.startswith(f"name: {prefix}")), -1)


# A plan that deletes or replaces anything stops the run unless the person
# starting it ticked allow_destroy.
check(
    re.search(r"\n      allow_destroy:\n(?:        .*\n)*?        default: false\n(?:        .*\n)*?        type: boolean", triggers),
    "deploy-infrastructure.yml: needs a boolean 'allow_destroy' input that defaults to false",
)
i_plan, i_gate, i_apply = step_index("Plan"), step_index("Check the plan for deletions"), step_index("Apply infrastructure")
check(
    -1 not in (i_plan, i_gate, i_apply) and i_plan < i_gate < i_apply,
    f"apply job: 'Check the plan for deletions' must sit between 'Plan' and 'Apply infrastructure'; order {titles}",
)
gate = apply_steps[i_gate] if i_gate >= 0 else ""
check(
    "terraform show -json tfplan" in gate
    and 'index("delete")' in gate
    and re.search(r'if \[\[ "\$ALLOW_DESTROY" != "true" \]\]; then\n(?:.*\n)*?\s+exit 1\n', gate)
    and "ALLOW_DESTROY: ${{ inputs.allow_destroy }}" in gate,
    "apply job: the deletion check must read 'terraform show -json tfplan', find actions containing "
    "\"delete\", and exit 1 unless ALLOW_DESTROY (from inputs.allow_destroy) is true",
)
check(
    re.search(APPLY_IF, "\n" + gate) or "\n        if:" not in "\n" + gate,
    "apply job: the deletion check must run whenever the apply runs",
)
check(
    "continue-on-error" not in apply_job and "continue-on-error" not in plan_job,
    "deploy-infrastructure.yml: no step may continue on error",
)

# An unreadable state bucket is an error, not "never built": skipping the
# plan would hide every change the pull request makes.
locate = next((st for st in steps_of(plan_job) if st.startswith("name: Locate Terraform state")), "")
check(
    re.search(r'\n {10}aws s3api head-bucket --bucket "\$BUCKET"\n', locate),
    "plan job: Locate Terraform state must run 'aws s3api head-bucket' on its own line, so any error fails the step",
)
check(
    "(404)" in locate and re.search(r"else\n(?:.*\n)*?\s+exit 1\n", locate),
    "plan job: only a 404 on the state key may mean 'not built yet'; any other error must exit 1",
)
for name, text in infra_workflows.items():
    for line in text.splitlines():
        if "head-object" in line:
            check(
                not re.search(r"2>&1|2>/dev/null", line) or "2>&1 >/dev/null" in line,
                f"{name}: head-object must not discard its error (403 would read as 'absent'): '{line.strip()}'",
            )

# The account's one GitHub OIDC provider is shared with the live deploys. The
# workflows' roles may not change it, so Terraform must only refer to it: a
# guessed "true" would let a stack own it, and destroying that stack would
# delete every GitHub sign-in in the account.
for name, text in all_workflows.items():
    check("CREATE_GITHUB_OIDC_PROVIDER" not in text, f"{name}: must not read CREATE_GITHUB_OIDC_PROVIDER")
    for value in re.findall(r"TF_VAR_create_github_oidc_provider:\s*(.+)", text):
        check(
            value.strip().strip("'\"") == "false",
            f"{name}: TF_VAR_create_github_oidc_provider must be literally false, got '{value.strip()}'",
        )
check(
    "CREATE_GITHUB_OIDC_PROVIDER" not in (TERRAFORM / "README.md").read_text(),
    "README.md: the CREATE_GITHUB_OIDC_PROVIDER secret is gone; the workflows always use the existing provider",
)
check(
    re.search(r"TF_VAR_create_github_oidc_provider:\s*'?false", deploy)
    and re.search(r"TF_VAR_create_github_oidc_provider:\s*'?false", destroy),
    "deploy and destroy workflows must set TF_VAR_create_github_oidc_provider to false",
)

# On a pull request without the plan role (or from a fork), every step after
# the configuration check must skip, not fail.
plan_steps = steps_of(plan_job)
for st in plan_steps[1:]:
    check(
        re.search(r"\n        if: steps\.(config\.outputs\.configured|state\.outputs\.found) == 'true'\n", st),
        f"plan job, step '{st.splitlines()[0]}': must be skipped unless the plan role is configured",
    )
# The apply job always signs in with its own role.
creds = [st for st in apply_steps if "aws-actions/configure-aws-credentials" in st]
check(
    len(creds) == 1 and "\n        if:" not in creds[0]
    and "role-to-assume: ${{ secrets.TF_APPLY_ROLE_ARN }}" in creds[0],
    "apply job: one unconditional configure-aws-credentials step assuming secrets.TF_APPLY_ROLE_ARN",
)

# Prose must not promise more than the workflow does.
reference = (ROOT / "docs" / "deployment" / "technical-reference.rst").read_text()
check(
    "stops with a lock error" not in reference.split("``production.yml``")[0]
    or "waits" in reference.split("``production.yml``")[0],
    "technical-reference.rst: a second run on the same environment now waits (concurrency), say so",
)
check(
    "nothing else" not in reference.split("``production.yml``")[0],
    "technical-reference.rst: the targeted ACM apply runs before the plan; do not say the apply does nothing else",
)

# --- IAM roles for Terraform: committed, and checked here ---------------------
# The Terraform roles live in the same AWS account as the hand-built live
# deployment. Each role trusts one GitHub Environment on one branch, reaches
# only its own environment's state and resources, cannot change IAM beyond its
# own names (and only under a permissions boundary), cannot change the shared
# GitHub OIDC provider, and is explicitly denied the live resources.

IAM = TERRAFORM / "bootstrap" / "iam"
REPO_SUBJECTS = ("repo:wwbp/chatbotlab", "repo:wwbp@42522115/chatbotlab@882137210")
OIDC = "token.actions.githubusercontent.com"
OIDC_ARN = f"arn:aws:iam::<ACCOUNT_ID>:oidc-provider/{OIDC}"
STATE_BUCKET = "arn:aws:s3:::<PREFIX>-tfstate-<ACCOUNT_ID>"
EB_BUCKET = "arn:aws:s3:::elasticbeanstalk-us-east-1-<ACCOUNT_ID>"
LIVE_EB_ENVIRONMENTS = {"humanlikebot-env", "humanlike-bot-development-env"}
# One ARN of each live resource, as AWS would see it in a request.
LIVE_SAMPLES = [
    "arn:aws:elasticbeanstalk:us-east-1:<ACCOUNT_ID>:application/humanlikebot",
    "arn:aws:elasticbeanstalk:us-east-1:<ACCOUNT_ID>:environment/humanlikebot/humanlikebot-env",
    "arn:aws:elasticbeanstalk:us-east-1:<ACCOUNT_ID>:environment/humanlikebot/humanlike-bot-development-env",
    "arn:aws:elasticbeanstalk:us-east-1:<ACCOUNT_ID>:applicationversion/humanlikebot/app-1",
    "arn:aws:rds:us-east-1:<ACCOUNT_ID>:db:awseb-e-abc123-stack-awsebrdsdatabase-xyz",
    "arn:aws:rds:us-east-1:<ACCOUNT_ID>:snapshot:awseb-e-abc123-snapshot",
    "arn:aws:cloudfront::<ACCOUNT_ID>:distribution/E2Q6P4TGU4GOC",
    "arn:aws:cloudfront::<ACCOUNT_ID>:distribution/E2XKRGQ6R0OG6W",
    f"{EB_BUCKET}/humanlikebot/app-1.zip",
    *(f"arn:aws:s3:::{b}{suffix}" for b in ("bot.wwbp.org", "dev.bot.wwbp.org", "bot-avatar.wwbp.org",
                                            "dev.bot-avatar.wwbp.org") for suffix in ("", "/index.html")),
]
OIDC_WRITES = [
    "iam:CreateOpenIDConnectProvider", "iam:DeleteOpenIDConnectProvider",
    "iam:UpdateOpenIDConnectProviderThumbprint", "iam:AddClientIDToOpenIDConnectProvider",
    "iam:RemoveClientIDFromOpenIDConnectProvider", "iam:TagOpenIDConnectProvider",
    "iam:UntagOpenIDConnectProvider",
]
EB_MANAGED_POLICIES = {
    "arn:aws:iam::aws:policy/AWSElasticBeanstalkWebTier",
    "arn:aws:iam::aws:policy/service-role/AWSElasticBeanstalkEnhancedHealth",
    "arn:aws:iam::aws:policy/AWSElasticBeanstalkManagedUpdatesCustomerRolePolicy",
    "arn:aws:iam::aws:policy/AdministratorAccess-AWSElasticBeanstalk",
}
ENVIRONMENTS = ("staging", "production")
# Services whose resources Elastic Beanstalk names itself, so the Terraform
# roles reach them by tag (Environment / Project), not by name.
TAG_SCOPED_SERVICES = {"ec2", "autoscaling", "elasticloadbalancing", "cloudformation", "cloudfront", "acm", "cloudwatch"}
# Calls with no resource to tag: nothing narrower than "*" exists.
UNTAGGABLE_ACTIONS = {"cloudwatch:PutMetricData"}
OTHER = {"staging": "production", "production": "staging"}
BRANCH = {"staging": "refs/heads/staging", "production": "refs/heads/main"}
# role -> (environment it serves, GitHub Environment it trusts, ref condition)
ROLES = {
    # Pull requests plan staging only: until secrets leave the state, no
    # pull-request code may run with a role that reads production's state.
    # Production is planned by the apply job (action = plan), from main.
    "plan-staging": ("staging", "plan-staging", ("StringLike", "refs/pull/*/merge")),
    **{f"apply-{e}": (e, e, ("StringEquals", BRANCH[e])) for e in ENVIRONMENTS},
}


ROLE_PARTS = {"plan": ("permissions",), "apply": ("permissions", "abac", "guardrails")}


def boundary_arn(env_name):
    return f"arn:aws:iam::<ACCOUNT_ID>:policy/<PREFIX>-terraform-boundary-{env_name}"


def load_policy(name):
    path = IAM / f"{name}.json"
    check(path.exists(), f"bootstrap/iam/{name}.json is missing")
    if not path.exists():
        return {"Statement": []}
    text = path.read_text()
    check("ReadOnlyAccess" not in text, f"bootstrap/iam/{name}.json: must not use ReadOnlyAccess")
    policy = json.loads(text)
    statements = policy["Statement"]
    return {**policy, "Statement": statements if isinstance(statements, list) else [statements]}


def as_list(value):
    return value if isinstance(value, list) else [value]


def matching(policy, effect, action, resource):
    """Statements of `effect` whose Action and Resource patterns match (IAM
    wildcards; conditions not evaluated)."""
    return [
        st for st in policy["Statement"]
        if st.get("Effect") == effect
        and any(fnmatchcase(action.lower(), a.lower()) for a in as_list(st.get("Action", [])))
        and any(fnmatchcase(resource, r) for r in as_list(st.get("Resource", [])))
    ]


def allows(policy, action, resource):
    """True when an Allow statement grants `action` on `resource` and no
    unconditional Deny refuses it."""
    denied = any("Condition" not in st for st in matching(policy, "Deny", action, resource))
    return bool(matching(policy, "Allow", action, resource)) and not denied


def denies(policy, action, resource):
    return any("Condition" not in st for st in matching(policy, "Deny", action, resource))


def sample_arns(env_name):
    """One ARN per kind of resource the stack of `env_name` owns."""
    p = f"<PREFIX>-{env_name}"
    return {
        "state": f"{STATE_BUCKET}/<PREFIX>/{env_name}/terraform.tfstate",
        "bucket": f"arn:aws:s3:::{p}-data",
        "object": f"arn:aws:s3:::{p}-data/avatar/x.png",
        "eb-version": f"{EB_BUCKET}/{p}/app-1.zip",
        "rds": f"arn:aws:rds:us-east-1:<ACCOUNT_ID>:db:{p}-database",
        "cache": f"arn:aws:elasticache:us-east-1:<ACCOUNT_ID>:serverlesscache:{p}-redis",
        "eb-app": f"arn:aws:elasticbeanstalk:us-east-1:<ACCOUNT_ID>:application/{p}",
        "eb-env": f"arn:aws:elasticbeanstalk:us-east-1:<ACCOUNT_ID>:environment/{p}/{p}-env",
        "role": f"arn:aws:iam::<ACCOUNT_ID>:role/{p}-eb-instance-role",
        "param": f"arn:aws:ssm:us-east-1:<ACCOUNT_ID>:parameter/{p}/DJANGO_SECRET_KEY",
        "secret": f"arn:aws:secretsmanager:us-east-1:<ACCOUNT_ID>:secret:{p}/database-AbCdEf",
    }


# IAM actions that accept the iam:PermissionsBoundary condition key: every one
# of them must require this environment's boundary.
BOUNDARY_KEYED = {"iam:CreateRole", "iam:PutRolePermissionsBoundary", "iam:AttachRolePolicy",
                  "iam:DetachRolePolicy", "iam:PutRolePolicy", "iam:DeleteRolePolicy"}
# Service-wide actions a boundary may allow on every resource, and why: the EB
# service role's managed updates and `eb deploy` drive these on resources EB
# names itself (awseb-e-*), so they cannot be scoped by name; the
# Environment / live-tag conditions still exclude the other environment and
# the live deployment.
BOUNDARY_WILDCARDS_ON_ANY_RESOURCE = {"ec2:*", "autoscaling:*", "elasticloadbalancing:*", "cloudformation:*"}


# --- Tag scoping (ABAC) rules for statements on Resource "*" ------------------
# A negated tag condition passes for an untagged resource, and the hand-built
# live resources carry no Environment/Project tags: conditions must name the
# own environment positively. aws:RequestTag says nothing about WHICH existing
# resource is acted on, so it is enough only for actions that create one;
# tagging actions could pull a live resource into the environment, so they
# need both, plus a limit on the tag keys.
TAGGING_ACTIONS = {
    "ec2:CreateTags", "elasticloadbalancing:AddTags", "autoscaling:CreateOrUpdateTags", "cloudfront:TagResource",
    "acm:AddTagsToCertificate", "cloudformation:TagResource", "cloudwatch:TagResource",
}
# Tag-on-create: the service authorises the tagging call for a resource that
# does not exist yet, and names the creating call in this key.
CREATE_ACTION_KEYS = {"ec2:CreateTags": "ec2:CreateAction", "elasticloadbalancing:AddTags": "elasticloadbalancing:CreateAction"}
# The stack's own tag keys, plus those Elastic Beanstalk and CloudFormation
# add to what they create (patterns, for ForAllValues:StringLike).
ALLOWED_TAG_KEYS = {"Environment", "ManagedBy", "Name", "Project", "Purpose", "Tier",
                    "elasticbeanstalk:*", "aws:cloudformation:*"}
CREATING_VERB = re.compile(r"(Create|Run|Allocate|Request|Import)")


def own_tags(cond, kind, environment, operators=("StringEquals",)):
    return any(cond.get(op, {}).get(f"aws:{kind}/Environment") == environment
               and cond.get(op, {}).get(f"aws:{kind}/Project") == "<PREFIX>" for op in operators)


def tag_keys_limited(cond):
    keys = [k for op in ("ForAllValues:StringEquals", "ForAllValues:StringLike")
            for k in as_list(cond.get(op, {}).get("aws:TagKeys", []))]
    return bool(keys) and set(keys) <= ALLOWED_TAG_KEYS


def tag_scope_problems(st, environment):
    """Why an Allow statement on Resource "*" is not scoped to its own
    environment's tags (empty when it is)."""
    if st.get("Effect") != "Allow":
        return []
    cond = st.get("Condition", {})
    problems = [f"negated condition {op} fails open on untagged resources" for op in cond if "Not" in op]
    if "*" not in as_list(st.get("Resource", [])):
        return problems
    resource_tag = own_tags(cond, "ResourceTag", environment)
    request_tag = own_tags(cond, "RequestTag", environment) and tag_keys_limited(cond)
    for action in as_list(st.get("Action", [])):
        service, _, verb = action.partition(":")
        if service not in TAG_SCOPED_SERVICES or action in UNTAGGABLE_ACTIONS:
            continue
        if re.match(r"(Describe|Get|List)[A-Za-z]*\*?$", verb) and "*" != verb:
            continue
        covered_tagging = [t for t in TAGGING_ACTIONS if fnmatchcase(t, action)]
        if covered_tagging:
            tag_on_create = all(CREATE_ACTION_KEYS.get(t) and any(CREATE_ACTION_KEYS[t] in c for c in cond.values())
                                for t in covered_tagging)
            both = resource_tag and own_tags(cond, "RequestTag", environment, ("StringEquals", "StringEqualsIfExists")) \
                and tag_keys_limited(cond)
            if not (both or (tag_on_create and request_tag)):
                problems.append(f"'{action}' can tag ({', '.join(sorted(covered_tagging))}): needs aws:ResourceTag "
                                f"and aws:RequestTag Environment = {environment}, Project = <PREFIX>, and aws:TagKeys "
                                f"(or, for tag-on-create, the CreateAction key with aws:RequestTag and aws:TagKeys)")
        elif "*" not in verb and CREATING_VERB.match(verb):
            if not (request_tag or resource_tag):
                problems.append(f"'{action}' creates: needs aws:RequestTag Environment = {environment}, "
                                f"Project = <PREFIX> and aws:TagKeys")
        elif not resource_tag:
            problems.append(f"'{action}' on '*' needs aws:ResourceTag Environment = {environment} and Project = <PREFIX>")
    return problems


# The validator itself: statements that must be refused, and ones it must pass.
_tags = lambda kind, op="StringEquals": {op: {f"aws:{kind}/Environment": "staging", f"aws:{kind}/Project": "<PREFIX>"}}
_keys = {"ForAllValues:StringEquals": {"aws:TagKeys": ["Environment", "Project"]}}
for label, st, ok in (
    ("terminate scoped by RequestTag only", {"Effect": "Allow", "Action": "ec2:TerminateInstances", "Resource": "*",
                                              "Condition": {**_tags("RequestTag"), **_keys}}, False),
    ("ec2:* scoped by RequestTag only", {"Effect": "Allow", "Action": "ec2:*", "Resource": "*",
                                         "Condition": {**_tags("RequestTag"), **_keys}}, False),
    ("ec2:* scoped by ResourceTag (covers CreateTags)", {"Effect": "Allow", "Action": "ec2:*", "Resource": "*",
                                                         "Condition": _tags("ResourceTag")}, False),
    ("ec2:Create* by RequestTag (covers CreateTags)", {"Effect": "Allow", "Action": "ec2:Create*", "Resource": "*",
                                                       "Condition": {**_tags("RequestTag"), **_keys}}, False),
    ("CreateTags by ResourceTag only", {"Effect": "Allow", "Action": "ec2:CreateTags", "Resource": "*",
                                        "Condition": _tags("ResourceTag")}, False),
    ("CreateTags without TagKeys", {"Effect": "Allow", "Action": "ec2:CreateTags", "Resource": "*",
                                    "Condition": {"StringEquals": {**_tags("ResourceTag")["StringEquals"],
                                                                   **_tags("RequestTag")["StringEquals"]}}}, False),
    ("RunInstances without TagKeys", {"Effect": "Allow", "Action": "ec2:RunInstances", "Resource": "*",
                                      "Condition": _tags("RequestTag")}, False),
    ("negated tag condition", {"Effect": "Allow", "Action": "ec2:Delete*", "Resource": "*",
                               "Condition": {"StringNotEquals": {"aws:ResourceTag/Environment": "production"}}}, False),
    ("other environment's tags", {"Effect": "Allow", "Action": "ec2:Delete*", "Resource": "*",
                                  "Condition": {"StringEquals": {"aws:ResourceTag/Environment": "production",
                                                                 "aws:ResourceTag/Project": "<PREFIX>"}}}, False),
    ("RunInstances with RequestTag and TagKeys", {"Effect": "Allow", "Action": "ec2:RunInstances", "Resource": "*",
                                                  "Condition": {**_tags("RequestTag"), **_keys}}, True),
    ("ec2:Delete* by ResourceTag", {"Effect": "Allow", "Action": "ec2:Delete*", "Resource": "*",
                                    "Condition": _tags("ResourceTag")}, True),
    ("tag-on-create", {"Effect": "Allow", "Action": "ec2:CreateTags", "Resource": "*",
                       "Condition": {"StringEquals": {**_tags("RequestTag")["StringEquals"],
                                                      "ec2:CreateAction": ["RunInstances"]}, **_keys}}, True),
    ("retag own resource", {"Effect": "Allow", "Action": "ec2:CreateTags", "Resource": "*",
                            "Condition": {**_tags("ResourceTag"), **_tags("RequestTag", "StringEqualsIfExists"), **_keys}}, True),
):
    check(bool(tag_scope_problems(st, "staging")) != ok,
          f"tag_scope_problems: fixture '{label}' should be {'accepted' if ok else 'refused'}")


def is_service_wide_write(action):
    service, _, verb = action.partition(":")
    return action == "*" or (verb.endswith("*") and not re.match(r"(Describe|Get|List|Check)", verb))


policies = {}
for role, (environment, gh_environment, (ref_operator, ref)) in ROLES.items():
    trust = load_policy(f"{role}-trust")
    statements = trust["Statement"]
    check(len(statements) == 1, f"{role}-trust.json: needs exactly one statement")
    st = statements[0] if statements else {}
    condition = st.get("Condition", {})
    expected_subjects = {f"{repo}:environment:{gh_environment}" for repo in REPO_SUBJECTS}
    check(
        st.get("Effect") == "Allow"
        and st.get("Action") == "sts:AssumeRoleWithWebIdentity"
        and st.get("Principal") == {"Federated": OIDC_ARN},
        f"{role}-trust.json: must allow only sts:AssumeRoleWithWebIdentity from the GitHub OIDC provider",
    )
    check(
        set(condition) <= {"StringEquals", "StringLike"}
        and condition.get("StringEquals", {}).get(f"{OIDC}:aud") == "sts.amazonaws.com",
        f"{role}-trust.json: audience must be sts.amazonaws.com",
    )
    subjects = set(as_list(condition.get("StringEquals", {}).get(f"{OIDC}:sub", [])))
    check(
        subjects == expected_subjects and f"{OIDC}:sub" not in condition.get("StringLike", {}),
        f"{role}-trust.json: subject must be exactly {sorted(expected_subjects)} (StringEquals), "
        f"got {sorted(subjects)}",
    )
    check("pull_request" not in json.dumps(trust), f"{role}-trust.json: must not trust the pull_request subject")
    check(
        as_list(condition.get(ref_operator, {}).get(f"{OIDC}:ref", [])) == [ref],
        f"{role}-trust.json: needs {ref_operator} {OIDC}:ref = {ref}",
    )
    # A role's permissions may be split over several managed policies
    # (<role>-permissions.json, <role>-abac.json, <role>-guardrails.json); the
    # checks below look at all of them together.
    parts = [load_policy(f"{role}-{part}") for part in ROLE_PARTS[role.split("-")[0]]]
    policies[role] = {"Statement": [st for part in parts for st in part["Statement"]]}

for env_name in ENVIRONMENTS:
    policies[f"boundary-{env_name}"] = load_policy(f"boundary-{env_name}")
check(
    not (set(p.stem for p in IAM.glob("*.json"))
         - {f"{r}-{k}" for r in ROLES for k in ("trust", *ROLE_PARTS[r.split("-")[0]])}
         - {f"boundary-{e}" for e in ENVIRONMENTS}) if IAM.exists() else True,
    "bootstrap/iam/: holds a policy file this check does not know about",
)

# IAM size limits (characters without whitespace), with the placeholders
# replaced by long real values: a policy that fits only with short ones fails
# at setup time.
def iam_size(path):
    text = json.dumps(json.loads(path.read_text()), separators=(",", ":"))
    return len(text.replace("<PREFIX>", "x" * 24).replace("<SES_FROM_DOMAIN>", "x" * 30))


for path in sorted(IAM.glob("*.json")) if IAM.exists() else []:
    # Trust policy: 2048. Everything else is a customer managed policy: 6144.
    limit = 2048 if path.stem.endswith("-trust") else 6144
    check(iam_size(path) <= limit,
          f"bootstrap/iam/{path.name}: {iam_size(path)} characters with a 24-character prefix; the IAM limit is {limit}")

live_statements = {}
for name, policy in policies.items():
    environment = name.rsplit("-", 1)[-1]
    fname = name if name.startswith("boundary") else f"{name}-permissions"
    other = OTHER[environment]
    kind = name.split("-", 1)[0]
    for st in policy["Statement"]:
        check(
            not {"NotAction", "NotResource", "NotPrincipal"} & set(st),
            f"{fname}.json: NotAction/NotResource are too easy to get wrong; list what is allowed",
        )
        if st.get("Effect") != "Allow":
            continue
        for action in as_list(st.get("Action", [])):
            check(action not in ("*", "iam:*"), f"{fname}.json: '{action}' is never allowed")
        for problem in tag_scope_problems(st, environment):
            check(False, f"{fname}.json: {problem}")

    # Live deployment: denied by name, and by tag where EB names resources
    # unpredictably (instances, security groups, load balancers, Auto Scaling
    # groups, the awseb-e-* CloudFormation stacks).
    missing = sorted(arn for arn in LIVE_SAMPLES if not denies(policy, "*", arn))
    check(not missing, f"{fname}.json: must deny everything on the live resources; missing {missing}")
    tag_denies = [
        st for st in policy["Statement"]
        if st.get("Effect") == "Deny" and as_list(st.get("Action")) == ["*"] and as_list(st.get("Resource")) == ["*"]
        and set(as_list(st.get("Condition", {}).get("StringEquals", {})
                        .get("aws:ResourceTag/elasticbeanstalk:environment-name", []))) == LIVE_EB_ENVIRONMENTS
    ]
    check(tag_denies, f"{fname}.json: must deny everything tagged with a live elasticbeanstalk:environment-name")
    live_statements[name] = sorted(
        json.dumps(st, sort_keys=True) for st in policy["Statement"]
        if st.get("Sid") in ("DenyHandBuiltLiveDeployment", "DenyHandBuiltLiveEnvironmentResources",
                             "DenyOidcProviderChanges")
    )

    # The shared OIDC provider: readable, never writable.
    for action in OIDC_WRITES:
        check(not matching(policy, "Allow", action, OIDC_ARN),
              f"{fname}.json: no statement may allow {action} on the OIDC provider (even if a Deny overrides it)")
        check(denies(policy, action, OIDC_ARN), f"{fname}.json: must explicitly deny {action}")

    # The other environment: the Terraform roles are denied it by name and by
    # tag. The boundary (size-limited to 6144 characters) instead allows
    # nothing that names it: every Allow there is scoped to its own names or
    # its own Environment tag.
    if kind == "boundary":
        for what, arn in sample_arns(other).items():
            named = [r for st in policy["Statement"] if st.get("Effect") == "Allow"
                     for r in as_list(st.get("Resource", [])) if r != "*" and fnmatchcase(arn, r)]
            check(not named, f"{fname}.json: an Allow reaches the {other} {what} ({arn}) through {named}")
    else:
        for what, arn in sample_arns(other).items():
            check(denies(policy, "s3:GetObject", arn), f"{fname}.json: must deny the {other} {what} ({arn})")
        check(
            any(st.get("Effect") == "Deny" and as_list(st.get("Action")) == ["*"]
                and as_list(st.get("Condition", {}).get("StringEquals", {}).get("aws:ResourceTag/Environment", [])) == [other]
                for st in policy["Statement"]),
            f"{fname}.json: must deny everything tagged Environment={other}",
        )

    if kind in ("plan", "apply"):
        check(not allows(policy, "s3:GetObject", "arn:aws:s3:::someone-elses-bucket/key"),
              f"{fname}.json: must not read objects in buckets that are not its own")
    if kind == "plan":
        own = sample_arns(environment)
        check(not allows(policy, "s3:GetObject", own["object"]),
              f"{fname}.json: must not read application data (only its state file)")
        check(allows(policy, "s3:GetObject", own["state"]), f"{fname}.json: must read its own state file")
        for st in policy["Statement"]:
            if st.get("Effect") != "Allow":
                continue
            for action in as_list(st.get("Action", [])):
                check(re.match(r"(Describe|Get|List|Head)", action.split(":", 1)[-1]),
                      f"{fname}.json: '{action}' is not read-only")
            for resource in as_list(st.get("Resource", [])):
                check(
                    not (resource.startswith("arn:aws:s3:::") and "/" in resource) or resource == own["state"],
                    f"{fname}.json: may read one S3 object, its own state file; found '{resource}'",
                )
    elif kind == "apply":
        own = sample_arns(environment)
        for action in ("s3:GetObject", "s3:PutObject", "s3:DeleteObject"):
            check(allows(policy, action, own["state"]), f"{fname}.json: {action} on its own state must be allowed")
            check(not allows(policy, action, f"{STATE_BUCKET}/<PREFIX>/terraform.tfstate"),
                  f"{fname}.json: {action} on the old project-wide state key must not be allowed")
        # Name-scoped services: its own resources yes, anyone else's no.
        for action, what in (("rds:DeleteDBInstance", "rds"), ("elasticache:DeleteServerlessCache", "cache"),
                             ("elasticbeanstalk:TerminateEnvironment", "eb-env"),
                             ("elasticbeanstalk:DeleteApplication", "eb-app"), ("s3:PutObject", "eb-version"),
                             ("s3:DeleteBucket", "bucket")):
            check(allows(policy, action, own[what]), f"{fname}.json: must allow {action} on its own {what}")
            check(not allows(policy, action, own[what].replace(f"<PREFIX>-{environment}", "someone-else")),
                  f"{fname}.json: {action} must be limited to <PREFIX>-{environment} names")
        # The shared Elastic Beanstalk bucket: only this application's keys.
        for key in ("humanlikebot/app.zip", "resources/environments/e-live/x", f"<PREFIX>-{other}/app.zip"):
            check(not allows(policy, "s3:PutObject", f"{EB_BUCKET}/{key}"),
                  f"{fname}.json: must not write '{key}' in the shared elasticbeanstalk bucket")
        # IAM: only the actions Terraform needs, only on its own names, only
        # under the boundary, attaching only the EB managed policies.
        for st in policy["Statement"]:
            if st.get("Effect") != "Allow":
                continue
            actions = as_list(st.get("Action", []))
            for action in actions:
                if not action.startswith("iam:") or re.match(r"iam:(Get|List)", action):
                    continue
                check("*" not in action, f"{fname}.json: IAM actions must be named, not '{action}'")
                for resource in as_list(st.get("Resource", [])):
                    check(
                        re.fullmatch(rf"arn:aws:iam::<ACCOUNT_ID>:(role|instance-profile)/<PREFIX>-{environment}-\*", resource)
                        or (action == "iam:CreateServiceLinkedRole" and ":role/aws-service-role/" in resource),
                        f"{fname}.json: {action} may act only on <PREFIX>-{environment}-* roles and instance profiles, not '{resource}'",
                    )
                cond = st.get("Condition", {})
                if action in BOUNDARY_KEYED:
                    check(cond.get("StringEquals", {}).get("iam:PermissionsBoundary") == boundary_arn(environment),
                          f"{fname}.json: {action} needs iam:PermissionsBoundary = {boundary_arn(environment)}")
                check(action != "iam:UpdateAssumeRolePolicy",
                      f"{fname}.json: iam:UpdateAssumeRolePolicy could make a role assumable by anyone; not allowed")
                if action in ("iam:AttachRolePolicy", "iam:DetachRolePolicy"):
                    arns = set(as_list(cond.get("ArnEquals", {}).get("iam:PolicyARN", [])))
                    check(arns and arns <= EB_MANAGED_POLICIES,
                          f"{fname}.json: {action} must be limited to the EB managed policies, got {sorted(arns)}")
                if action == "iam:PassRole":
                    services = set(as_list(cond.get("StringEquals", {}).get("iam:PassedToService", [])))
                    check(services and services <= {"elasticbeanstalk.amazonaws.com", "ec2.amazonaws.com"},
                          f"{fname}.json: iam:PassRole needs iam:PassedToService = EB / EC2")
        check(allows(policy, "iam:CreateRole", own["role"]), f"{fname}.json: must be able to create its own roles")
        check(
            any(st.get("Effect") == "Deny" and "iam:PutRolePermissionsBoundary" in as_list(st.get("Action"))
                and st.get("Condition", {}).get("StringNotEquals", {}).get("iam:PermissionsBoundary") == boundary_arn(environment)
                for st in policy["Statement"]),
            f"{fname}.json: must deny iam:PutRolePermissionsBoundary to any boundary but {boundary_arn(environment)}",
        )
        for action in ("iam:DeleteRolePermissionsBoundary", "iam:CreatePolicyVersion", "iam:DeletePolicy",
                       "iam:SetDefaultPolicyVersion"):
            check(denies(policy, action, boundary_arn(environment)) or denies(policy, action, own["role"]),
                  f"{fname}.json: must deny {action} (the boundary must stay in place)")
        for target in ("role/<PREFIX>-terraform-apply-production", "role/<PREFIX>-terraform-plan-staging",
                       "role/aws-elasticbeanstalk-ec2-role", f"role/<PREFIX>-{other}-github-actions"):
            check(not allows(policy, "iam:PutRolePolicy", f"arn:aws:iam::<ACCOUNT_ID>:{target}"),
                  f"{fname}.json: must not change {target}")
    else:  # boundary: the most any role the stack creates can ever do
        own = sample_arns(environment)
        for st in policy["Statement"]:
            if st.get("Effect") != "Allow":
                continue
            for action in as_list(st.get("Action", [])):
                check(not action.startswith(("sns:", "sqs:")), f"{fname}.json: the stack uses no SNS/SQS; '{action}'")
                if action.endswith(":*") and "*" in as_list(st.get("Resource", [])):
                    check(action in BOUNDARY_WILDCARDS_ON_ANY_RESOURCE,
                          f"{fname}.json: '{action}' on '*' is wider than the stack's roles need")
        for action, arn, expected in (
            ("ssm:GetParameter", own["param"], True),
            ("ssm:GetParameter", "arn:aws:ssm:us-east-1:<ACCOUNT_ID>:parameter/someone-else/x", False),
            ("ses:SendEmail", "arn:aws:ses:us-east-1:<ACCOUNT_ID>:identity/<SES_FROM_DOMAIN>", True),
            ("ses:SendEmail", "arn:aws:ses:us-east-1:<ACCOUNT_ID>:identity/someone-else.org", False),
            ("s3:GetObject", own["object"], True),
            ("s3:PutObject", own["eb-version"], True),
            ("s3:GetObject", "arn:aws:s3:::someone-elses-bucket/key", False),
            ("s3:PutObject", f"{EB_BUCKET}/someone-else/app.zip", False),
        ):
            check(allows(policy, action, arn) == expected,
                  f"{fname}.json: {action} on {arn} must be {'allowed' if expected else 'refused'}")
        check(denies(policy, "s3:GetObject", sample_arns(environment)["state"]),
              f"{fname}.json: must explicitly deny the Terraform state to the roles the stack creates")
        for action in ("iam:CreateRole", "iam:PutRolePolicy", "iam:AttachRolePolicy", "iam:CreatePolicyVersion",
                       "iam:CreateUser", "iam:CreateAccessKey"):
            check(not allows(policy, action, "arn:aws:iam::<ACCOUNT_ID>:role/<PREFIX>-x"),
                  f"{fname}.json: roles the stack creates must not be able to {action}")

check(len({json.dumps(v) for v in live_statements.values()}) == 1 and all(len(v) == 3 for v in live_statements.values()),
      "bootstrap/iam: every policy must carry the same three live/OIDC Deny statements")

# The pull-request plan runs only after a reviewer approves it in the
# plan-<environment> GitHub Environment, the subject its role trusts.
check(
    "\n    environment: plan-${{ matrix.environment }}\n" in "\n" + plan_job,
    "plan job: must run in the 'plan-<environment>' GitHub Environment (required reviewers approve each run)",
)
check(
    re.search(r"\n      matrix:\n        environment: \[staging\]\n", plan_job) and "production" not in plan_job,
    "plan job: pull requests plan staging only; no pull-request code may get a role that reads production state",
)
readme_text = (TERRAFORM / "README.md").read_text()
check(
    re.search(r"`plan-staging`[^\n]*\n?[^\n]*\*\*Required reviewers\*\*[^.]*\*\*Prevent self-review\*\*", readme_text)
    and "plan-production" not in readme_text,
    "README.md: plan-staging needs required reviewers with Prevent self-review; there is no plan-production",
)
# The boundary is mandatory: no switch turns it off.
for name, text in {**all_workflows, "variables.tf": (TERRAFORM / "variables.tf").read_text()}.items():
    check("use_permissions_boundary" not in text, f"{name}: the permissions boundary must not be optional")
check(
    "put-role-permissions-boundary" in (TERRAFORM / "README.md").read_text(),
    "README.md: must say to attach the boundary to existing stack roles before granting the apply policy",
)

readme = (TERRAFORM / "README.md").read_text()
check("ReadOnlyAccess" not in readme, "README.md: the plan role must not get ReadOnlyAccess")
check(":pull_request" not in readme, "README.md: no role may trust the pull_request subject")
check(
    re.search(r"- `production`: \*\*Required reviewers\*\*, \*\*Prevent self-review\*\* ticked", readme),
    "README.md: production needs required reviewers with 'Prevent self-review'",
)
check(
    not re.search(r"Deployment branches\*\*[^.]*`staging`[^.]*production|production[^.]*Deployment branches\*\*[^.]*`staging`", readme),
    "README.md: production may deploy only from main",
)
check("bootstrap/iam/" in readme, "README.md: setup must point at the committed policies in bootstrap/iam/")

# --- Destroy: manual, one named environment, production needs a second yes ---

destroy_triggers = destroy.split("\njobs:\n", 1)[0]
check(
    re.findall(r"\n  (\w+):", "\n" + destroy_triggers.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]) == ["workflow_dispatch"],
    "destroy-infrastructure.yml: may be started only by workflow_dispatch",
)
check(
    re.search(r"\n      environment:\n(?:        .*\n)*?        type: string", destroy_triggers),
    "destroy-infrastructure.yml: the environment must be typed, not picked from a list (a list preselects its first entry)",
)
check(
    re.search(r"\n      allow_production:\n(?:        .*\n)*?        default: false", destroy_triggers)
    and re.search(r"\n      allow_production:\n(?:        .*\n)*?        type: boolean", destroy_triggers),
    "destroy-infrastructure.yml: needs an 'allow_production' boolean input that defaults to false",
)
destroy_jobs = jobs_of(destroy)
check(set(destroy_jobs) == {"validate", "destroy"}, f"destroy-infrastructure.yml: expected jobs validate and destroy, found {sorted(destroy_jobs)}")
validate_job = destroy_jobs.get("validate", "")
destroy_job = destroy_jobs.get("destroy", "")
# The inputs are checked in a job of their own, before the job that names a
# GitHub Environment: GitHub creates an Environment for any name it is given,
# and a new one has no reviewers.
check("\n    environment:" not in "\n" + validate_job, "destroy validate job: must not use a GitHub Environment")
check("role-to-assume" not in validate_job, "destroy validate job: must not sign in to AWS")
for needle, why in (
    ('"$ENVIRONMENT" != "staging" && "$ENVIRONMENT" != "production"', "reject any environment other than staging or production"),
    ('"$CONFIRM_DESTROY" != "destroy $ENVIRONMENT"', "require the typed confirmation 'destroy <environment>'"),
    ('"$ENVIRONMENT" == "production" && "$ALLOW_PRODUCTION" != "true"', "refuse production unless allow_production is set"),
):
    check(needle in validate_job, f"destroy validate job: must {why}")
check(
    re.search(r"\n    needs: \[?validate\]?\n", "\n" + destroy_job),
    "destroy job: must depend on the validate job",
)
check("role-to-assume" in destroy_job, "destroy job: must sign in through an OIDC role")

# --- Stale files and statements ----------------------------------------------

check(
    not (WORKFLOWS / "test-iac.yml").exists(),
    "test-iac.yml deployed directories that no longer exist, from a branch that does not exist",
)
for name, text in all_workflows.items():
    check(
        # (generic_chatbot.settings, the Django module, is still real.)
        not re.search(r"generic_chatbot_frontend|generic_chatbot\b(?!\.)|REACT_APP_", text),
        f"{name}: refers to directories or variables the repository no longer has",
    )
readme = (TERRAFORM / "README.md").read_text()
check(
    "test-iac" not in readme and "tf-iac" not in readme,
    "README.md: refers to the removed test-iac workflow or the tf-iac branch",
)
reference = (ROOT / "docs" / "deployment" / "technical-reference.rst").read_text()
rds_tf = (TERRAFORM / "rds.tf").read_text()
if re.search(r'backup_retention_period\s*=\s*var\.environment == "production" \? 7 : 1', rds_tf):
    check(
        "backup_retention_period = 0" not in reference and "backups are disabled" not in reference,
        "technical-reference.rst: says backups are disabled, but rds.tf keeps 7 days (production) / 1 day (staging)",
    )

if failures:
    print("\n".join(f"FAIL: {f}" for f in failures))
    sys.exit(1)
print("workflow guard checks passed")
