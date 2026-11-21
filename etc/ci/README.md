# CI

`.github/workflows/test.yml` runs `just ci` (tidy, `zig build test`, `zig build check`) on
GitHub Actions on every push to `main`. It runs only what the push can break:

- `plan` diffs the pushed tree against the tree of the last commit whose run of this
  workflow passed on the branch (`baseline.sh`, which says why that is safe) and keeps the
  steps of every changed package, of every package that imports one, and of the tools that
  import one. It deals them to as many shards as they fill (at most 12). A sharded package
  whose runs outweigh its compile and that costs more than a shard carries goes to several
  shards as groups, `test:<package>@K/G` (build.zig `test_group_counts`), each of which
  compiles the binary and runs a contiguous part of its shards. A push that changes
  no package plans none; no green run yet, a path the plan cannot place, or a change to the
  build, the plan or the toolchain plans every step. A plan of one shard runs in the plan job
  itself, so a leaf edit is one job.
- `tidy` runs `etc/scripts/tidy.sh` on every push, beside the plan.
- `shard N` runs when the plan has two shards or more: its steps in one
  `zig build <steps> --summary all`.

The run's conclusion is the status. AGENTS.md rule 1 has every change pushed straight to
`main`, with no pull requests, so the workflow has no single check for a pull request to
require. A plan of no shard or one skips the `shard` job, and GitHub reports the skipped
matrix as one check named `shard ${{ matrix.shard }}`, so no shard check can be required.
To require a check, add a job `done` with `needs: [plan, tidy, shard]` and `if: always()`
that fails unless `plan` and `tidy` succeeded and `shard` succeeded or was skipped, and
require `done`.

The workflow is thin; the logic lives here and in `etc/scripts/`:

- `build.zig` makes `test` and `check` depend only on named steps: `test:<package>`,
  `check:<package>` and its `ci_steps` list. It fails to configure otherwise, and when the
  groups `test:<package>@K/G` of one G do not run each shard of the package exactly once.
- `etc/scripts/ci-shards.py` lists those steps (`steps`), plans a push (`plan BASE HEAD 12`),
  deals steps to shards largest cost first (`shard INDEX COUNT`), rebuilds the cost table from
  `--summary all` logs (`costs LOG...`) and checks that the shards cover every step exactly
  once and that the Justfile's `ci`, `tidy`, `test` and `check` recipes still say what the
  shards run and that its settings and module lines are the pinned ones (`check`, run by
  `just tidy`).
  The workflow runs the same commands as `just ci` but does not run `just`, so it does not
  prove that the Justfile or the module it loads parses.
- `baseline.sh` prints the last green commit of the branch: the plan's BASE.
- `costs.tsv` holds, per step, the seconds of its compile and of its runs (one per shard of
  a sharded package), measured on GitHub's `ubuntu-latest` runners. A group costs the
  compile plus its shards' runs.
- `shard.sh` runs one shard's steps.

Refresh the cost table after a large change to the suite: save the job logs of a green run
and run `python3 -I etc/scripts/ci-shards.py costs <logs>`.

A push that changes `.github/workflows/` needs a token with workflow scope (or ssh); the
automation token has none, so agents change only the files above. `ci.yml` (release-safe
tests, simulator swarm, fuzz) is a template that is not enabled; to enable it, copy it to
`.github/workflows/`. Run the same checks locally with `just ci` and `just test-linux`.
