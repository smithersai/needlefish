# CI

`.github/workflows/test.yml` runs `just ci` (tidy, `zig build test`, `zig build check`) on
GitHub Actions on every push to `main`, split into 12 parallel jobs. The workflow is thin; the
logic lives here and in `etc/scripts/`:

- `build.zig` makes `test` and `check` depend only on named steps: `test:<package>`,
  `check:<package>` and its `ci_steps` list. It fails to configure otherwise.
- `etc/scripts/ci-shards.py` lists those steps (`steps`), deals them to shards largest cost
  first (`shard INDEX COUNT`), rebuilds the cost table from `--summary all` logs
  (`costs LOG...`) and checks that the shards cover every step exactly once and that the
  Justfile's `ci`, `tidy`, `test` and `check` recipes still say what the shards run and that its settings and module lines are the pinned ones (`check`,
  run by `just tidy`).
  The workflow runs the same commands as `just ci` but does not run `just`, so it does not
  prove that the Justfile or the module it loads parses.
- `costs.tsv` holds seconds per step, measured on GitHub's `ubuntu-latest` runners.
- `shard.sh` runs one shard's steps: tidy, then one `zig build <steps> --summary all`.

Refresh the cost table after a large change to the suite: save the job logs of a green run
and run `python3 -I etc/scripts/ci-shards.py costs <logs>`.

A push that changes `.github/workflows/` needs a token with workflow scope (or ssh); the
automation token has none, so agents change only the files above. `ci.yml` (release-safe
tests, simulator swarm, fuzz) is a template that is not enabled; to enable it, copy it to
`.github/workflows/`. Run the same checks locally with `just ci` and `just test-linux`.
