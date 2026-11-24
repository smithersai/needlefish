#!/usr/bin/env bash
# Delete every GitHub Actions cache entry of REPOSITORY whose key starts with PREFIX except
# KEEP, so one entry per prefix stays and the repository's 10 GB never evicts by age.
# usage: GH_TOKEN=<token with actions: write> etc/ci/prune-caches.sh REPOSITORY PREFIX KEEP
set -euo pipefail
repository=$1 prefix=$2 keep=$3
[ -n "$prefix" ] && [ "${keep#"$prefix"}" != "$keep" ] || {
  echo "prune-caches: KEEP must start with a non-empty PREFIX" >&2
  exit 1
}

deleted=0
ids=$(gh cache list -R "$repository" --key "$prefix" --limit 100 --json id,key \
  --jq ".[] | select(.key != \"$keep\") | .id")
for id in $ids; do
  gh cache delete -R "$repository" "$id"
  deleted=$((deleted + 1))
done
echo "prune-caches: $prefix: kept $keep, deleted $deleted" >&2
