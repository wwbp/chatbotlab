#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# setup.sh — one-command deployment of the entire ChatbotLab infrastructure
#
# What this script does, in order:
#   1. Checks that the tools I need (terraform, aws CLI) are installed
#   2. Checks that AWS credentials are configured
#   3. Checks that terraform.tfvars exists (copy from terraform.tfvars.example)
#   4. Runs the bootstrap step to create the S3 state bucket (skips if
#      already done)
#   5. Runs `terraform init` pointing at the backend created in step 4
#   6. Runs `terraform apply` to provision all infrastructure
#   7. Prints the GitHub Actions secrets you need to configure
#
# How to run:
#   cd terraform
#   cp terraform.tfvars.example terraform.tfvars
#   # Edit terraform.tfvars with your actual values
#   bash setup.sh
#
# To deploy a second environment (e.g. production), set environment="production"
# in terraform.tfvars and run again. Each environment has its own state file
# (<project_name>/<environment>/terraform.tfstate) in the same bucket.
# -----------------------------------------------------------------------------

set -euo pipefail

# Colour helpers — make the output readable at a glance
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
RESET='\033[0m'

info()    { echo -e "${GREEN}[INFO]${RESET}  $1"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $1"; }
header()  { echo -e "\n${BOLD}${BLUE}=== $1 ===${RESET}"; }
die()     { echo -e "${RED}[ERROR]${RESET} $1" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# -----------------------------------------------------------------------------
# Step 1 — Check prerequisites
# -----------------------------------------------------------------------------

header "Checking prerequisites"

command -v terraform >/dev/null 2>&1 \
  || die "terraform is not installed. Install it from https://developer.hashicorp.com/terraform/install"
info "terraform $(terraform version -json | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"terraform_version\"])')"

command -v aws >/dev/null 2>&1 \
  || die "aws CLI is not installed. Install it from https://aws.amazon.com/cli/"
info "aws CLI $(aws --version 2>&1 | cut -d' ' -f1)"

# -----------------------------------------------------------------------------
# Step 2 — Check AWS credentials
# -----------------------------------------------------------------------------

header "Checking AWS credentials"

CALLER_IDENTITY=$(aws sts get-caller-identity 2>&1) \
  || die "AWS credentials are not configured or have expired. Run: aws configure"

AWS_ACCOUNT=$(echo "$CALLER_IDENTITY" | python3 -c 'import sys,json; print(json.load(sys.stdin)["Account"])')
AWS_CALLER=$(echo "$CALLER_IDENTITY" | python3 -c 'import sys,json; print(json.load(sys.stdin)["Arn"])')
info "Connected as: $AWS_CALLER (account $AWS_ACCOUNT)"

# -----------------------------------------------------------------------------
# Step 3 — Check terraform.tfvars
# -----------------------------------------------------------------------------

header "Checking configuration"

TFVARS="$SCRIPT_DIR/terraform.tfvars"
[[ -f "$TFVARS" ]] \
  || die "terraform.tfvars not found. Copy the example and fill it in:\n  cp terraform.tfvars.example terraform.tfvars"

# Warn if the user has not replaced placeholder values
if grep -q "REPLACE_with" "$TFVARS"; then
  die "terraform.tfvars still contains placeholder values (lines with REPLACE_with). Fill them in before running setup."
fi

info "terraform.tfvars found and appears filled in"

# -----------------------------------------------------------------------------
# Step 4 — Bootstrap (creates state backend if it does not exist yet)
# -----------------------------------------------------------------------------

header "Bootstrapping Terraform state backend"

BOOTSTRAP_DIR="$SCRIPT_DIR/bootstrap"
BACKEND_HCL="$SCRIPT_DIR/backend.hcl"

cd "$BOOTSTRAP_DIR"

# project_name and environment have no defaults (they name live AWS resources,
# so guessing them is unsafe). Read the values the operator already set in
# terraform.tfvars, so the state bucket, the state key and the main config agree.
PROJECT_NAME=$(grep -E '^\s*project_name\s*=' "$TFVARS" | head -1 | cut -d'"' -f2)
ENVIRONMENT=$(grep -E '^\s*environment\s*=' "$TFVARS" | head -1 | cut -d'"' -f2)
[[ -n "$PROJECT_NAME" ]] \
  || die "project_name is not set in $TFVARS — add it before running setup."
[[ "$ENVIRONMENT" == "staging" || "$ENVIRONMENT" == "production" ]] \
  || die "environment in $TFVARS must be \"staging\" or \"production\"."

if [[ -f "$BACKEND_HCL" ]]; then
  info "backend.hcl already exists — skipping bootstrap (state backend is already provisioned)"
else
  info "Running bootstrap to create the S3 state bucket..."
  terraform init -reconfigure -input=false
  terraform apply -auto-approve -input=false -var="project_name=$PROJECT_NAME"
  info "Bootstrap complete. backend.hcl written to $BACKEND_HCL"
fi

# -----------------------------------------------------------------------------
# Step 5 — Initialise the main configuration with remote state
# -----------------------------------------------------------------------------

header "Initialising Terraform with remote state"

cd "$SCRIPT_DIR"

# One state file per environment. A backend.hcl written by an older bootstrap
# still carries a project-wide key; the key given here overrides it. The state
# lock is set in main.tf.
terraform init \
  -backend-config="$BACKEND_HCL" \
  -backend-config="key=${PROJECT_NAME}/${ENVIRONMENT}/terraform.tfstate" \
  -reconfigure \
  -input=false

info "Terraform initialised (state: ${PROJECT_NAME}/${ENVIRONMENT}/terraform.tfstate)"

# -----------------------------------------------------------------------------
# Step 6 — Plan then apply
# -----------------------------------------------------------------------------

header "Planning infrastructure changes"

terraform plan \
  -var-file="$TFVARS" \
  -out=tfplan \
  -input=false

echo ""
warn "Review the plan above. Press Enter to apply, or Ctrl+C to cancel."
read -r

header "Applying infrastructure"

terraform apply \
  -input=false \
  tfplan

rm -f tfplan

# -----------------------------------------------------------------------------
# Step 7 — Print GitHub Actions secrets
# -----------------------------------------------------------------------------

header "Done! Configure these GitHub Actions secrets"

echo ""
GITHUB_ORG=$(grep 'github_org' "$TFVARS" | head -1 | sed 's/.*=[ ]*"\(.*\)".*/\1/')
GITHUB_REPO=$(grep 'github_repo' "$TFVARS" | head -1 | sed 's/.*=[ ]*"\(.*\)".*/\1/')
echo -e "${BOLD}Go to: https://github.com/${GITHUB_ORG:-<your-org>}/${GITHUB_REPO:-<your-repo>}/settings/secrets/actions${RESET}"
echo ""
echo "Copy these values into your repository secrets ($ENVIRONMENT only — the"
echo "other environment's deploy workflow reads different names):"
echo ""

# The same (secret, output) pairs the Deploy Infrastructure workflow writes;
# check_workflows.py keeps the two lists equal.
show() {
  VALUE=$(terraform output -raw "github_secret_$2" 2>/dev/null || echo "(run terraform output github_secret_$2)")
  printf "  ${BOLD}%-50s${RESET} %s\n" "$1" "$VALUE"
}
case "$ENVIRONMENT" in
  staging)
    show AWS_ACCOUNT_ID                 AWS_ACCOUNT_ID
    show AWS_ROLE_NAME                  AWS_ROLE_NAME
    show AWS_S3_BUCKET_NAME_FRONTEND    AWS_S3_BUCKET_NAME_FRONTEND
    show AWS_CLOUDFRONT_DISTRIBUTION_ID AWS_CLOUDFRONT_DISTRIBUTION_ID
    show AWS_BACKEND_APPLICATION_NAME   AWS_BACKEND_APPLICATION_NAME
    show AWS_BACKEND_ENVIRONMENT_NAME   AWS_BACKEND_ENVIRONMENT_NAME
    ;;
  production)
    show AWS_PROD_ACCOUNT_ID                 AWS_ACCOUNT_ID
    show AWS_PROD_ROLE_NAME                  AWS_ROLE_NAME
    show AWS_S3_PROD_BUCKET_NAME_FRONTEND    AWS_S3_BUCKET_NAME_FRONTEND
    show AWS_PROD_CLOUDFRONT_DISTRIBUTION_ID AWS_CLOUDFRONT_DISTRIBUTION_ID
    show AWS_PROD_BACKEND_APPLICATION_NAME   AWS_BACKEND_APPLICATION_NAME
    show AWS_PROD_BACKEND_ENVIRONMENT_NAME   AWS_BACKEND_ENVIRONMENT_NAME
    ;;
esac

echo ""
info "Frontend URL: $(terraform output -raw frontend_url)"
info "Backend URL:  $(terraform output -raw backend_api_url)"
echo ""
info "Next step: deploy the app — push to the staging branch (staging) or main (production)."
