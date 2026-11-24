# CI

Two GitHub Actions workflows run on this repository. `.github/workflows/test.yml` runs
`just ci` (tidy, `zig build test`, `zig build check`) on every push to `main` and every pull
request, in at most 6 jobs. `.github/workflows/nightly.yml` runs, once a day on `main`, what
the push path leaves out: the whole Debug suite from an empty cache, ReleaseSafe tests, the
simulator swarm, fuzzing and macOS; and it saves the two caches `test.yml` reads.

## test.yml

- `plan` diffs the pushed tree against the tree of the last commit whose push run of this
  workflow passed on `main` (`baseline.sh`, which says why that is safe; a pull request uses
  its base branch). It keeps tidy, the steps of every changed package, of every package that
  imports one, and of the tools that import one, prices them (`ci-shards.py` `weighted`) and
  deals them to as many shards as they fill at 48 weighted seconds each, at most 6. A plan of
  one shard runs in the plan job itself, so a docs, CI-plumbing or leaf edit is one job.
- `shard N` runs when the plan has two shards or more: its steps in one
  `zig build <steps> --summary all`, after restoring the nightly compile cache.

What a path plans (`python3 -I etc/scripts/ci-shards.py preview 6 PATH...` prints it):

| Change | Plans |
| --- | --- |
| `docs/`, `.smithers/`, `etc/` outside `etc/ci/`, `AGENTS.md`, `README.md`, `Justfile` | tidy |
| CI plumbing: `.github/` outside `.github/actions/zig/`, `etc/ci/`, `ci-shards.py`, `packages.py` | tidy and `test:common/rlp` |
| A package under `src/` or `bin/` | tidy, it, its importers, the tools that import it |
| `build.zig`, `build.zig.zon`, `packages.zon`, `src/testing/test_runner.zig`, `install-zig.sh`, `.github/actions/zig/`, or an unknown path | every step |

CI plumbing plans one cheap package, not every step: tidy runs `ci-shards.py check` (the
planner's self-tests, every shard count, the Justfile), the plan job runs the planner and
`baseline.sh`, and `test:common/rlp` runs `shard.sh`, the build runner and `groups`. A step
of a full plan whose binary the nightly cache already holds is a hit, its test runs included.

Why at most 6 jobs: the `smithersai` organization is on GitHub Free. Its repositories share
20 concurrent jobs, and the organization has no larger hosted runners (the API answers "GitHub
hosted runners are not supported for this organization"). On 2026-11-22 from 12:36 to 13:15
UTC `smithers` held 20 or 21 of those slots, and a 16-shard run of this workflow (run
45023253379) started its shards between 1 and 29 minutes after the plan, one or two at a time.
Each job also pays about 10 s of checkout and Zig setup. Fewer, fuller jobs wait for fewer
slots. Tidy rides in a shard instead of a job of its own.

Why one job per sharded package: a sharded package (build.zig `test_shards`) runs its binary
as N processes, which a job's 4 processors run side by side. Its groups
`test:<package>@K/G` would let several jobs share it, but each would compile the binary again
(`test:testing/sim` compiles for 18 s), so `plan` never deals them; `groups` still checks
build.zig declares the groups the planner knows.

Why `weighted`: over 397 shard jobs of 40 green runs, a job's `steps` took 0.50 s per
weighted second plus 6 s at the median CPU model when an LLVM compile (build.zig
`llvm_test_packages`, `optimized_test_packages`) weighs 1.2 per table second, Zig's own
backend 0.4, and a run 1.0: a median error of 13.3%, against 16.6% for the plain sum. The rest
of the skew between shards is the runner: the same plan ran 1.8 times longer on an AMD EPYC
7763 than on an AMD EPYC 9V45 or Intel Xeon 6975P (median actual over planned, 0.78 against
0.44), and GitHub picks the runner.

The run's conclusion is the status. A plan of one shard skips the `shard` job, and GitHub
reports the skipped matrix as one check named `shard ${{ matrix.shard }}`, so no shard check
can be required. To require a check, add a job `done` with `needs: [plan, shard]` and
`if: always()` that fails unless `plan` succeeded and `shard` succeeded or was skipped, and
require `done`.

## nightly.yml fuzz

`fuzz` runs as 6 jobs, `etc/ci/fuzz.sh GROUP 6 RUN`. `ci-shards.py fuzz GROUP 6` deals the
packages with a coverage-guided target (`testing.fuzz(`, `fuzz.run(`, `fuzz.run_bytes(`) and
those whose only fuzz tests are seeded by their ReleaseSafe compile seconds (`fuzz-costs.tsv`),
which puts at most two of the 6 GB, 6 to 8 minute compiles in one `zig build`. Each target gets 20000 inputs (FUZZ_RUNS: 2000
for execution/node and testing/sim, 5000 for execution/rpc and execution/db: execution/node takes
28 inputs a second, the simulator's cluster target about 2), after
its package's tests ran once with the seeded soak (`-Dfuzz-soak=50 -Dfuzz-seed=RUN`); the
seeded-only packages run the soak alone. A failing job uploads the input Zig saved.

Why: the first nightly ran `zig build test --fuzz=200000` and then the soak over `zig build
test` as one job, which printed nothing for 120 minutes and was cancelled (run 45311462122).
`--fuzz` builds every test binary in ReleaseSafe, runs every test with the shards off, builds
each fuzzed binary again instrumented (all of one `zig build` at once, 6 GB each for the
heaviest), and then fuzzes each target for N inputs: execution/node takes 28 inputs a second,
so N = 200000 alone is about 30 hours. The release-safe job's one ReleaseSafe build and
sharded run of every package took 102 minutes. Measured on a 4-processor box, 2026-11-24.

Zig 0.17.1 exits 0 when a fuzz process fails, so `fuzz.sh` fails the job when the log says
`input saved to`. src/testing/fuzz.zig skips its seeded driver in the instrumented build
(`builtin.fuzz`): the fuzzer runs a test again for every batch, and the soak already ran.

## Caches

- Compile cache (`.zig-cache`, `.github/actions/zig` `local-cache: restore`). Zig's cache is
  content-addressed: a compile, or a test run, whose inputs match a manifest is a hit, whatever
  the files' timestamps. `nightly.yml` `warm` runs every step from an empty cache, prunes it
  (`prune-zig-cache.sh`; at most 2048 MiB or it is not saved), saves it, and deletes the older
  entries (`prune-caches.sh`), so one entry stays. Shards restore the newest; nothing else
  saves one. CI builds test binaries for `ZIG_CPU=x86_64_v3` (`shard.sh`), not the native CPU,
  so the cache serves every runner: 45 sampled jobs ran on six CPU models.
- Cost table. `nightly.yml` measures it from the newest 10 green push runs
  (`refresh-costs.sh`, `ci-shards.py costs`) and saves it; `plan` reads it through
  `CI_SHARDS_COSTS` and falls back to the committed `costs.tsv` when it is missing or does not
  parse. A step that ran from the compile cache is not measured.

## Files

The workflows are thin; the logic lives here and in `etc/scripts/`:

- `build.zig` makes `test` and `check` depend only on named steps: `test:<package>`,
  `check:<package>` and its `ci_steps` list. It fails to configure otherwise, and when the
  groups `test:<package>@K/G` of one G do not run each shard of the package exactly once.
- `etc/scripts/ci-shards.py` lists those steps (`steps`), plans a push (`plan BASE HEAD 6`) or
  a list of paths (`preview 6 PATH...`), deals steps to shards largest cost first (`shard INDEX
  COUNT`), rebuilds the cost table from `--summary all` logs (`costs LOG...`) and checks that
  the shards cover every step exactly once and that the Justfile's `ci`, `tidy`, `test` and
  `check` recipes still say what the shards run and that its settings and module lines are the
  pinned ones (`check`, run by `just tidy`), and that the groups `zig build -l` lists are the
  ones it knows (`groups LISTING`, run by each job that runs Zig steps). `fuzz INDEX COUNT`
  lists a nightly fuzz group's steps; `check` also proves the groups hold each fuzz step once.
  The workflow runs the same commands as `just ci` but does not run `just`, so it does not
  prove that the Justfile or the module it loads parses.
- `baseline.sh` prints the last green commit of the branch: the plan's BASE.
- `costs.tsv` holds, per step, the seconds of its compile and of its runs (one per shard of
  a sharded package), measured on GitHub's `ubuntu-latest` runners. The nightly table
  overrides it in CI; refresh the committed one after a large change to the suite: save the
  job logs of a green run and run `python3 -I etc/scripts/ci-shards.py costs <logs>`.
- `shard.sh` runs one shard's steps.
- `fuzz.sh` runs one `fuzz` job of the nightly workflow (above).
- `fuzz-costs.tsv` holds each step's ReleaseSafe compile seconds, which deal the fuzz groups;
  refresh it from a green nightly's `release-safe` job log:
  `python3 -I etc/scripts/ci-shards.py fuzz-costs <log>`.
- `prune-zig-cache.sh`, `prune-caches.sh` and `refresh-costs.sh` keep the two caches.

A push that changes `.github/workflows/` needs a token with workflow scope (or ssh); an
automation token without it is rejected, so agents change only the files above and a
maintainer changes the workflows. Run the same checks locally with `just ci` and
`just test-linux`.
