#!/usr/bin/env bash
# One job of the nightly `fuzz` matrix (etc/ci/README.md): fuzz group INDEX of COUNT
# (`ci-shards.py fuzz`). Each `RUNS STEP...` line fuzzes its steps for RUNS inputs per target,
# after their tests ran once with the seeded fuzz soak (-Dfuzz-soak=50 -Dfuzz-seed=SEED); the
# `soak STEP...` line runs only the soak, of the tests named for fuzzing. Every line runs even
# if an earlier one failed. A failing soak replays with the same -Dfuzz-seed; a failing fuzz
# target with the input Zig saves in .zig-cache/f/crash.
# usage: etc/ci/fuzz.sh INDEX COUNT SEED
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
[ "$#" -eq 3 ] || { echo "usage: etc/ci/fuzz.sh INDEX COUNT SEED" >&2; exit 2; }
index=$1 count=$2 seed=$3
soak=(-Doptimize=ReleaseSafe -Dfuzz-soak=50 "-Dfuzz-seed=$seed")
log=$(mktemp)
trap 'rm -f "$log"' EXIT

fail=0
mapfile -t lines < <(python3 -I etc/scripts/ci-shards.py fuzz "$index" "$count")
for line in "${lines[@]}"; do
  read -r runs steps_text <<< "$line"
  read -r -a steps <<< "$steps_text"
  echo "fuzz: $runs: ${steps[*]}"
  start=$SECONDS
  failed=0
  if [ "$runs" = soak ]; then
    zig build "${steps[@]}" "${soak[@]}" -Dtest-filter=fuzz || failed=1
  else
    # Zig fuzzes one run step per binary, so the shards are off (build.zig `test_shards`).
    zig build "${steps[@]}" "${soak[@]}" -Dtest-shards=false --fuzz="$runs" 2>&1 \
      | tee "$log" || failed=1
    # Zig 0.17.1 exits 0 when a fuzz process fails; its error line names the saved input.
    if grep -q "input saved to '" "$log"; then failed=1; fi
  fi
  echo "fuzz: $runs: $((SECONDS - start)) s, failed=$failed"
  if [ "$failed" = 1 ]; then fail=1; fi
done
exit "$fail"
