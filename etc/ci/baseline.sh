#!/usr/bin/env bash
# Print the commit a push is planned against: the head of the newest run of this workflow on
# this branch that concluded success, fetched at depth 1. Print nothing, so every step is
# planned, when there is no such run, the API call fails or the commit cannot be fetched.
#
# Why that commit: a green run passed every step its diff from its own baseline could affect,
# and that baseline's run passed the rest, back to a run that planned every step. So every step
# passes on the baseline's tree, and only the steps this push's diff from it affects can fail.
# The commit before the push gives no such guarantee: its run may be red, cancelled or not yet
# done. The diff is tree to tree and needs no common history, so a force push is planned the
# same way: against the last green tree, whatever the branch's history now is.
# usage: GH_TOKEN=<token with actions: read> etc/ci/baseline.sh REPOSITORY BRANCH WORKFLOW
set -uo pipefail
repository=$1 branch=$2 workflow=$3

# The API's own status filter left out a green run minutes after it ended: filter the newest
# 100 runs here instead.
green='[.workflow_runs[] | select(.event == "push" and .conclusion == "success")][0].head_sha'
sha=$(gh api -X GET "repos/$repository/actions/workflows/$workflow/runs" -f branch="$branch" \
  -f per_page=100 --jq "$green // \"\"") || sha=
if [ -z "$sha" ]; then
  echo "ci-shards: no green run of $workflow on $branch: planning every step" >&2
elif ! git fetch --quiet --no-tags --depth=1 origin "$sha"; then
  echo "ci-shards: cannot fetch $sha: planning every step" >&2
else
  echo "ci-shards: planning against $sha, the last green run on $branch" >&2
  echo "$sha"
fi
