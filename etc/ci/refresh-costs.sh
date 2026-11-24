#!/usr/bin/env bash
# Measure the cost table from the newest RUNS green push runs of test.yml on main and write it
# to OUT/costs.tsv, which the nightly workflow saves for test.yml's plan (CI_SHARDS_COSTS).
# It reads the plan job's log and each shard's (`ci-shards.py costs`): a step no log measures,
# or that ran from the compile cache, keeps its committed etc/ci/costs.tsv value. Prints
# `saved=true` as a GitHub output when it wrote the table, nothing when no log was read.
# usage: GH_TOKEN=<token with actions: read> etc/ci/refresh-costs.sh REPOSITORY OUT RUNS
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
repository=$1 out=$2 runs_max=$3
[ "$runs_max" -ge 1 ] && [ "$runs_max" -le 50 ] || { echo "refresh-costs: RUNS 1..50" >&2; exit 1; }

logs=$(mktemp -d)
green="[.workflow_runs[] | select(.event == \"push\" and .conclusion == \"success\")]"
runs=$(gh api -X GET "repos/$repository/actions/workflows/test.yml/runs" -f branch=main \
  -f per_page=100 --jq "${green}[:${runs_max}][].id")
jobs='.jobs[] | select(.name == "plan" or (.name | test("^shard [0-9]+$"))) | .id'
for run in $runs; do
  for job in $(gh api "repos/$repository/actions/runs/$run/jobs?per_page=100" --jq "$jobs"); do
    gh api "repos/$repository/actions/jobs/$job/logs" > "$logs/$run-$job.log" ||
      rm -f "$logs/$run-$job.log"
  done
done

shopt -s nullglob
files=("$logs"/*.log)
if [ "${#files[@]}" -eq 0 ]; then
  echo "refresh-costs: no job log read: test.yml's plan keeps etc/ci/costs.tsv" >&2
  exit 0
fi
committed=$(mktemp)
cp etc/ci/costs.tsv "$committed"
python3 -I etc/scripts/ci-shards.py costs "${files[@]}" >&2
mkdir -p "$out"
cp etc/ci/costs.tsv "$out/costs.tsv"
cp "$committed" etc/ci/costs.tsv
changed=$(diff "$committed" "$out/costs.tsv" | grep -c '^>' || true)
echo "refresh-costs: ${#files[@]} logs; $changed rows differ from etc/ci/costs.tsv" >&2
echo "saved=true"
