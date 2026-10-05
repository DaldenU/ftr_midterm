#!/usr/bin/env bash
# Full measurement run: 7 failure scenarios x {baseline, ft} x REPS repetitions + reliability campaign.
# Takes ~45 minutes. Do not run other CPU-heavy work meanwhile (it would distort the measurements).
set -euo pipefail
cd "$(dirname "$0")/.."
REPS="${REPS:-3}"
python3 experiments/run_experiments.py --all --reps "$REPS" --clean
python3 experiments/run_campaign.py --duration "${CAMPAIGN_SECONDS:-240}" --mttf-c "${CAMPAIGN_MTTF_C:-15}"
python3 experiments/analysis/reliability.py experiments/results
python3 experiments/analysis/report_tables.py experiments/results
echo "ALL DONE"
