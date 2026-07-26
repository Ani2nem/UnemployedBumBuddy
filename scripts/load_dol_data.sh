#!/usr/bin/env bash
# Mac-friendly wrapper for pipeline.dol_lca_etl - same reasoning as seed.sh.
#
# Usage:
#   ./scripts/load_dol_data.sh --input ~/Downloads/LCA_Disclosure_Data_FY2026_Q2.xlsx --employers amazon google

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ ! -d "${REPO_ROOT}/.venv" ]; then
    echo "No .venv found at ${REPO_ROOT}/.venv - see scripts/seed.sh's error for setup steps." >&2
    exit 1
fi

source "${REPO_ROOT}/.venv/bin/activate"
export PYTHONPATH="${REPO_ROOT}/src"
export AWS_PROFILE="${AWS_PROFILE:-agent-deployer}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"

exec python3 -m pipeline.dol_lca_etl "$@"
