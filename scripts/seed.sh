#!/usr/bin/env bash
# Wrapper so `pipeline.seed_data` just works from a fresh terminal on this
# Mac - it needs the project venv active, `src/` on PYTHONPATH, and the
# AWS profile/region for this account set, none of which is the default
# shell state.
#
# Usage (from anywhere):
#   ./scripts/seed.sh profile --level SENIOR --countries US
#   ./scripts/seed.sh project --id my-project --title "..." --url https://... --description-file ./notes.md
#   ./scripts/seed.sh style-example --id example1 --tag cold-outreach --text-file ./example1.txt
#   ./scripts/seed.sh list --table projects

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ ! -d "${REPO_ROOT}/.venv" ]; then
    echo "No .venv found at ${REPO_ROOT}/.venv - run this first:" >&2
    echo "  cd ${REPO_ROOT} && python3 -m venv .venv && source .venv/bin/activate && pip install -e \".[adapters,pipeline,telegram,dev]\"" >&2
    exit 1
fi

source "${REPO_ROOT}/.venv/bin/activate"
export PYTHONPATH="${REPO_ROOT}/src"
export AWS_PROFILE="${AWS_PROFILE:-agent-deployer}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"

exec python3 -m pipeline.seed_data "$@"
