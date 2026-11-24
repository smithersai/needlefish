#!/usr/bin/env bash
# Prune a Zig local cache to what a later job's hits need, and print `save=true` when it fits
# in LIMIT MiB (`save=false` otherwise, with a warning), as a GitHub output.
#
# A hit needs the manifests (h/) and the outputs (o/): Zig checks a manifest's inputs and then
# uses o/<digest>/ as it is. It does not need the ZIR cache (z/), scratch files (tmp/) or the
# object an LLVM compile links its binary from (o/*/<name>_zcu.o): with Zig 0.17.1 a build of
# test:testing/sim and test:common/rlp hit in 0.2 s, against 15.5 s cold, after all three were
# deleted, and the cache shrank from 79 MiB to 22 MiB. Zig does not recreate tmp/, and a
# configure that misses fails with FileNotFound without it: it stays, empty.
# usage: etc/ci/prune-zig-cache.sh CACHE_DIR LIMIT_MIB
set -euo pipefail
cache=$1 limit_mib=$2
[ -d "$cache/h" ] && [ -d "$cache/o" ] || { echo "prune: $cache is no Zig cache" >&2; exit 1; }

before_mib=$(du -sm "$cache" | cut -f1)
rm -rf "$cache/z" "$cache/tmp"
mkdir "$cache/tmp"
find "$cache/o" -type f -name '*_zcu.o' -delete
after_mib=$(du -sm "$cache" | cut -f1)
echo "prune: $cache: $before_mib MiB, $after_mib MiB pruned, limit $limit_mib MiB" >&2
if [ "$after_mib" -le "$limit_mib" ]; then
  echo "save=true"
else
  echo "::warning::$cache is $after_mib MiB pruned, over $limit_mib MiB: not saved" >&2
  echo "save=false"
fi
