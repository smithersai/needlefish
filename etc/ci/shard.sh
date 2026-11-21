#!/usr/bin/env bash
# Run one CI shard's steps (etc/scripts/ci-shards.py): tidy if it is one of them, then every
# other step in one `zig build`, which runs them in parallel. Both run even if the first fails.
# usage: etc/ci/shard.sh STEP...
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

fail=0
zig_steps=()
for step in "$@"; do
  if [ "$step" = tidy ]; then
    start=$SECONDS
    bash etc/scripts/tidy.sh || fail=1
    # The cost table reads this line (etc/scripts/ci-shards.py costs).
    echo "ci-shards: tidy $((SECONDS - start))s"
  else
    zig_steps+=("$step")
  fi
done

if [ "${#zig_steps[@]}" -gt 0 ]; then
  flags=(--summary all -Drequire-io-uring=true)
  # Debug test binaries carry no debug info (build.zig `test_root_module`). A failing run runs
  # again with it, so the log shows file, line and stack, and the shard stays red.
  if ! zig build "${zig_steps[@]}" "${flags[@]}"; then
    zig build "${zig_steps[@]}" "${flags[@]}" -Dtest-debug-info=true || true
    fail=1
  fi
fi
exit "$fail"
