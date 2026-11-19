#!/usr/bin/env bash
# Slowest unit tests: installs every package's test binary (`zig build test-bins`), runs each
# without a TTY (the runner then prints "i/N name...OK" per test), timestamps each line,
# and lists the slowest.
# Usage: etc/scripts/test-times.sh [count] [extra zig build args...]
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
count="${1:-20}"
shift || true
rm -rf zig-out/test
zig build test-bins "$@" >/dev/null 2>&1 || true
ls zig-out/test/* >/dev/null 2>&1 || { echo "test-times: zig-out/test is empty"; exit 1; }
for binary in zig-out/test/*; do
  "$binary" 2>&1 </dev/null | perl -MTime::HiRes=time -ne '
    BEGIN { $|=1; $last = time; }
    # Each test prints "i/N name..." then its result; time from one result to the next.
    while (/(\d+)\/\d+ (.+?)\.\.\.(OK|SKIP|FAIL[^\n]*)/g) {
      $now = time; printf "%9.1f ms  %s %s\n", ($now - $last) * 1000, $2, ($3 eq "OK" ? "" : $3);
      $last = $now;
    }'
done | sort -rn | head -n "$count"
