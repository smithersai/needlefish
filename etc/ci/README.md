# CI

`.github/workflows/test.yml` runs `just ci` (tidy, `zig build test`, `zig build check`) on
GitHub Actions on every push to `main`. It runs only what the push can break:

- `plan` diffs the pushed tree against the tree of the last commit whose run of this
  workflow passed on the branch (`baseline.sh`, which says why that is safe) and keeps the
  steps of every changed package, of every package that imports one, and of the tools that
  import one. It deals them to as many shards as they fill (at most 12), so a leaf edit starts
  one shard and an edit of a package everything imports starts all of them. A push that
  changes no package starts none; no green run yet, a path the plan cannot place, or a change
  to the build, the plan or the toolchain starts every step.
- `tidy` runs `etc/scripts/tidy.sh` on every push, beside the plan.
- `shard N` runs its steps in one `zig build <steps> --summary all`.
- `done` passes when `plan`, `tidy` and every shard pass, or when the plan started no shard.
  Require `done` in branch protection: a matrix job the plan skips reports no per-shard
  check, so requiring `shard N` would block a docs-only push.

The workflow is thin; the logic lives here and in `etc/scripts/`:

- `build.zig` makes `test` and `check` depend only on named steps: `test:<package>`,
  `check:<package>` and its `ci_steps` list. It fails to configure otherwise.
- `etc/scripts/ci-shards.py` lists those steps (`steps`), plans a push (`plan BASE HEAD 12`),
  deals steps to shards largest cost first (`shard INDEX COUNT`), rebuilds the cost table from
  `--summary all` logs (`costs LOG...`) and checks that the shards cover every step exactly
  once and that the Justfile's `ci`, `tidy`, `test` and `check` recipes still say what the
  shards run and that its settings and module lines are the pinned ones (`check`, run by
  `just tidy`).
  The workflow runs the same commands as `just ci` but does not run `just`, so it does not
  prove that the Justfile or the module it loads parses.
- `baseline.sh` prints the last green commit of the branch: the plan's BASE.
- `costs.tsv` holds seconds per step, measured on GitHub's `ubuntu-latest` runners.
- `shard.sh` runs one shard's steps.

Refresh the cost table after a large change to the suite: save the job logs of a green run
and run `python3 -I etc/scripts/ci-shards.py costs <logs>`.

A push that changes `.github/workflows/` needs a token with workflow scope (or ssh); the
automation token has none, so agents change only the files above. `ci.yml` (release-safe
tests, simulator swarm, fuzz) is a template that is not enabled; to enable it, copy it to
`.github/workflows/`. Run the same checks locally with `just ci` and `just test-linux`.
