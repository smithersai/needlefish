#!/usr/bin/env python3
"""CI shards: `just ci` as named steps, dealt to parallel jobs by measured cost.

`just ci` is tidy, `zig build test` and `zig build check`. build.zig makes `test` and `check`
depend on named steps only (its `ci_steps` and `assert_named_dependencies`), so CI runs:
  tidy                 etc/scripts/tidy.sh
  test:<package>       each package `zig build test` runs (packages.zon, not compile_only); it
                       compiles the package's test binary, so it also covers check:<package>,
                       which holds nothing else (build.zig `assert_checks_covered`)
  check:<package>      each compile_only package: compiles its test binary
  ci_steps             build.zig's list: test:statetest and the check:<tool> steps

Groups: a package in build.zig's `test_shards` runs its test binary as N run steps
(`--shard=K/N`), and for each G of `test_group_counts` up to N, `test:<package>@K/G` (K from 1
to G) runs the contiguous shards (K-1)*N//G to K*N//G - 1 (`group_shards`). The groups of one G
run every shard once (build.zig `assert_groups_partition`). `plan` never deals groups: each job
that runs one compiles the binary again, and a job's 4 processors run a package's shards
side by side, so one job that compiles a binary once and runs all its shards beats two that
compile it twice (etc/ci/README.md). `groups` still checks build.zig's groups.

Costs: etc/ci/costs.tsv, per step, the seconds of its compiles and of its runs (one number per
shard for a package in `test_shards`): the durations in `zig build <steps> --summary all` logs,
summed (`costs`). The log prints whole seconds, and minutes for a step of a minute or more
(read as the minute plus 30 s). A step costs its compile plus its runs; a group costs the
compile plus its shards' runs. A step missing from the table costs DEFAULT_SECONDS, with a
warning; it is never dropped. `plan` reads CI_SHARDS_COSTS, a table the nightly workflow
refreshed from recent green runs, when it names a file that parses, and the committed table
otherwise. `plan` prices a compile by the backend that runs it (`weighted`): an LLVM compile
(build.zig `llvm_test_packages` and `optimized_test_packages`) at LLVM_COMPILE_WEIGHT of its
seconds, any other at SELF_COMPILE_WEIGHT, a run at its seconds.

`shard` deals the steps to COUNT shards: largest cost first, each to the shard with the least
cost so far (ties: the lowest index), so every job computes the same split.

`plan` keeps the steps a push needs: tidy, the packages whose files changed between BASE and
HEAD, every package that imports one of them (packages.zon), and the tools that import one. It
deals them to as many shards as they fill at SHARD_SECONDS each, at most LIMIT, and prints them
as GitHub outputs. BASE is the tree of the last commit CI passed (etc/ci/baseline.sh). An
unknown BASE, a change to a file in EVERYTHING, or a path the lists below cannot place plans
every step; a change to CI's own plumbing (CI_FILES, CI_DIRECTORIES) plans CANARY, one cheap
package that runs shard.sh end to end, since tidy already runs this file's `check`; a path in
NO_STEP_FILES or NO_STEP_DIRECTORIES plans no step but tidy.

usage: python3 -I etc/scripts/ci-shards.py steps              every CI step, one per line
       python3 -I etc/scripts/ci-shards.py shard INDEX COUNT  shard INDEX's steps (0-based)
       python3 -I etc/scripts/ci-shards.py plan BASE HEAD LIMIT   count= and matrix= outputs
       python3 -I etc/scripts/ci-shards.py preview LIMIT PATH...  plan a change to PATHs
                                                     (`-`: every step, as for an unknown BASE)
       python3 -I etc/scripts/ci-shards.py costs LOG...       rewrite etc/ci/costs.tsv from logs
       python3 -I etc/scripts/ci-shards.py check              the shards cover each step once
       python3 -I etc/scripts/ci-shards.py groups LISTING     build.zig's groups are these
       python3 -I etc/scripts/ci-shards.py fuzz INDEX COUNT   fuzz group INDEX (0-based)
       python3 -I etc/scripts/ci-shards.py fuzz-costs LOG...  rewrite etc/ci/fuzz-costs.tsv

`fuzz` prints fuzz group INDEX of COUNT (the nightly `fuzz` matrix, etc/ci/fuzz.sh): lines
`RUNS STEP...`, the test:<package> steps of the packages with a coverage-guided target
(`testing.fuzz(`, or `fuzz.run(`/`fuzz.run_bytes(` of src/testing/fuzz.zig) to fuzz for RUNS
inputs per target (FUZZ_RUNS), and a line `soak STEP...`, the packages whose only fuzz tests
are seeded. Steps are dealt by their ReleaseSafe compile seconds (FUZZ_COSTS, written by
`fuzz-costs` from a `zig build test -Doptimize=ReleaseSafe --summary all` log such as the
nightly `release-safe` job's): `--fuzz` builds every binary twice in ReleaseSafe, once
instrumented, and those compiles are most of a group's time. They do not follow the Debug
costs in COSTS or the code compiled: execution/rpc takes 5 s in Debug and 510 s in
ReleaseSafe, `testing` compiles more source than execution/node and takes a sixth as long.

`groups` reads LISTING, the output of `zig build -l`, where each group's description names its
shards, and fails unless build.zig declares exactly the groups `group_shards` prices: each side
proves its own partition, and only this comparison catches two groups whose ranges were
swapped. A CI job that runs Zig runs it (`zig build -l` is how it compiles the build runner);
`check` cannot, since tidy's job has no build runner and compiling one takes a minute.
"""
import collections
import contextlib
import functools
import io
import importlib.util
import json
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COSTS = 'etc/ci/costs.tsv'
FUZZ_COSTS = 'etc/ci/fuzz-costs.tsv'
DEFAULT_SECONDS = 10.0  # a step the table does not list yet
MIN_SECONDS = 0.1  # the least a measured step costs in the table, so none rounds to 0.0
MAX_SHARDS = 64  # `check` verifies every count up to this one
# The weighted cost (`weighted`) `plan` puts in one shard before it starts another. Each job
# more is one more wait for a runner, and runners are scarce: the smithersai organization runs
# on GitHub Free, whose 20 concurrent jobs every repository of the organization shares, and on
# 2026-11-22 from 12:36 to 13:15 UTC smithers held 20 or 21 of them, so a 16-shard run of this
# workflow started its last shard 29 minutes after its first. Over 397 shard jobs of 40 green
# runs a job's `steps` took 0.50 s per weighted second plus 6 s at the median CPU model, so a
# shard of 48 runs for about 30 s. A plan the limit caps is dealt to LIMIT shards whatever the
# budget.
SHARD_SECONDS = 48.0
# What a compile costs per table second, against a run's 1.0 (`weighted`). LLVM compiles one
# binary on one thread; Zig's own backend spreads one over the job's processors, so its table
# seconds, measured while a job's other steps compete, overstate what it holds the job for.
# These weights fit those 397 jobs' durations, scaled per CPU model, with a median error of
# 13.3%; the unweighted table had 16.6%.
LLVM_COMPILE_WEIGHT = 1.2
SELF_COMPILE_WEIGHT = 0.4
# What a part's chain, its compile plus its slowest run, which no processor of the job shares,
# weighs against the budget: the summed step seconds of a job were 1.6 times its work at the
# median over 294 job logs of GitHub's 4-vCPU runners, so a chain of c seconds fills c * 1.6
# of a shard. A part whose chain fills a shard is dealt at that weight, so the dealer gives it
# a shard of its own, unless every other shard is fuller.
CHAIN_WEIGHT = 1.6
CI_STEPS = re.compile(r'^const ci_steps = \[_\]\[\]const u8\{\n(.*?)^\};', re.M | re.S)
NAME = re.compile(r'\.name = "([^"]+)"')
NAME_LIST = r'^const %s = \[_\]\[\]const u8\{\n(.*?)^\};'
TEST_SHARDS = re.compile(r'^const test_shards = \[_\]Shards\{\n(.*?)^\};', re.M | re.S)
TEST_SHARD = re.compile(r'^    \.\{ \.package = "([^"]+)", \.count = (\d+) \},$', re.M)
GROUP_COUNTS = re.compile(r'^const test_group_counts = \[_\]u32\{ ([0-9, ]+) \};$', re.M)
GROUP = re.compile(r'^(test:.+)@(\d+)/(\d+)$')
# A group as `zig build -l` lists it: build.zig add_test_groups writes the description.
LISTED_GROUP = re.compile(r'^  (test:(\S+)@\d+/\d+) +Run test shards (\d+) to (\d+) of (\d+) '
                          r'of the (\S+) package$')
DURATION = re.compile(r' (\d+)(ns|us|ms|s|m)(?= |$)')
RUN_SHARD = re.compile(r'run test \S+ shard (\d+)/(\d+)')
TIMESTAMP = re.compile(r'^\d{4}-\d\d-\d\dT[\d:.]+Z ')
ANSI = re.compile(r'\x1b\[[0-9;]*m')
NODE = re.compile(r'^(?:\|  |   )*\+- (.*)$')
MEASURED = re.compile(r'^ci-shards: (\S+) (\d+)s$')
UNIT_SECONDS = {'ns': 1e-9, 'us': 1e-6, 'ms': 1e-3, 's': 1.0}
# Paths whose change reruns every step: what the build, the test runner and the toolchain are
# made of. A test binary such a change leaves alone hits the restored compile cache.
EVERYTHING = {'build.zig', 'build.zig.zon', 'packages.zon', 'etc/scripts/install-zig.sh',
              'src/testing/test_runner.zig'}
EVERYTHING_DIRECTORIES = ('.github/actions/zig/',)
# CI's own plumbing: it decides what runs, not what a test does. A change to one plans CANARY:
# tidy runs `check` (this file's self-tests, every shard count, the Justfile), the plan job
# runs the planner and baseline.sh, and CANARY runs shard.sh, the build runner and `groups`.
CI_FILES = {COSTS, 'etc/ci/shard.sh', 'etc/ci/baseline.sh', 'etc/scripts/ci-shards.py',
            'etc/scripts/packages.py'}
CI_DIRECTORIES = ('.github/', 'etc/ci/')
CANARY = 'test:common/rlp'
# Paths no step reads: a change to one plans no step but tidy (which reads etc/scripts/ and
# checks the Justfile). A path in none of these lists, outside src/ and bin/, plans every step.
NO_STEP_FILES = {'.gitignore', 'AGENTS.md', 'CLAUDE.md', 'Justfile', 'LICENSE', 'README.md'}
NO_STEP_DIRECTORIES = ('docs/', 'etc/', '.smithers/')
# `just ci` as the Justfile spells it: {recipe: (header, body lines)}. CI runs these as named
# steps: `tidy` as tidy, `zig build test` as every test:<package> and test:statetest (rerun with
# -Dtest-debug-info=true when it fails, as etc/ci/shard.sh does), `zig build check` as every
# check: step. `check` fails when the Justfile no longer says exactly this: change CI with it.
JUST_CI = {
    'ci': ('ci: tidy test check', []),
    'tidy': ('tidy:', ['etc/scripts/tidy.sh']),
    'test': ("test filter='':", [
        '#!/usr/bin/env bash',
        'set -euo pipefail',
        'if [ -n "{{ filter }}" ]; then zig build test -Dtest-filter="{{ filter }}"; exit; fi',
        'zig build test || { zig build test -Dtest-debug-info=true; exit 1; }',
    ]),
    'check': ('check:', ['zig build check']),
}
# The rest of the Justfile's top level as it is today. A setting changes how every recipe runs,
# and a module that does not load fails `just ci` before any recipe runs, so both are pinned
# and a module's file must exist. Besides these, comments and recipes, the top level may hold
# only aliases and variable assignments: any other line fails `check`.
JUST_SETTINGS = ['set positional-arguments := true']
JUST_MODULES = ["mod devnet 'etc/devnet/devnet.just'"]
JUST_TOP_LEVEL = re.compile(r"alias [\w-]+ := [\w-]+$|(?:export )?[A-Za-z_][\w-]* := "
                            r"|@?[A-Za-z_][\w-]*(?: [^:]*)?:(?: .*)?$")
# The package each of build.zig's ci_steps imports (`check` keeps this in step with ci_steps).
TOOLS = {
    'test:statetest': 'execution/evm',
    'check:simulator': 'base',
    'check:bench': 'base',
    'check:kzg-lines': 'base',
    'check:secp-tables': 'base',
}


def read(path):
    with open(os.path.join(ROOT, path), encoding='utf-8') as handle:
        return handle.read()


def packages():
    """{name: (deps, compile_only)} from packages.zon: a row runs from its `.name` to the next."""
    text, out = read('packages.zon'), {}
    names = list(NAME.finditer(text))
    for row, after in zip(names, names[1:] + [None]):
        block = text[row.end():after.start() if after else len(text)]
        deps = re.search(r'\.deps = \.\{(.*?)\}', block, re.S).group(1)
        out[row.group(1)] = (re.findall(r'"([^"]+)"', deps), '.compile_only = true' in block)
    assert len(out) == len(names), 'packages.zon names a package twice'
    return out


def ci_steps():
    """build.zig's `ci_steps`: the named steps besides test:<package> and check:<package>."""
    match = CI_STEPS.search(read('build.zig'))
    assert match, 'build.zig has no `const ci_steps = [_][]const u8{ ... };`'
    names = re.findall(r'^    "([^"]+)",$', match.group(1), re.M)
    assert names and len(names) == match.group(1).count('\n'), 'ci_steps: one name per line'
    return names


def package_step(name, compile_only):
    return ('check:' if compile_only else 'test:') + name


def steps():
    """Every CI step, sorted: tidy, one step per package, and build.zig's ci_steps."""
    out = ['tidy'] + ci_steps()
    out += [package_step(name, only) for name, (_, only) in packages().items()]
    assert len(set(out)) == len(out), 'a CI step is listed twice'
    return sorted(out)


@functools.cache
def test_shards():
    """{test:<package>: shard count} of build.zig's `test_shards`."""
    match = TEST_SHARDS.search(read('build.zig'))
    assert match, 'build.zig has no `const test_shards = [_]Shards{ ... };`'
    rows = TEST_SHARD.findall(match.group(1))
    assert rows and len(rows) == match.group(1).count('\n'), 'test_shards: one package per line'
    out = {'test:' + name: int(count) for name, count in rows}
    assert len(out) == len(rows) and min(out.values()) >= 2, 'test_shards: once each, count >= 2'
    return out


@functools.cache
def group_counts():
    """build.zig's `test_group_counts`: ascending, each at least 2."""
    match = GROUP_COUNTS.search(read('build.zig'))
    assert match, 'build.zig has no `const test_group_counts = [_]u32{ ... };`'
    out = [int(n) for n in match.group(1).split(', ')]
    assert out == sorted(set(out)) and out[0] >= 2, 'test_group_counts: ascending, each >= 2'
    return out


def group_shards(group, groups, count):
    """The shards (0-based) that group `group` (1-based) of `groups` runs, of a binary run as
    `count` shards: build.zig's add_test_groups computes the same range."""
    assert 1 <= group <= groups <= count
    return range((group - 1) * count // groups, group * count // groups)


def group_name(step, group, groups):
    return '%s@%d/%d' % (step, group, groups)


def read_costs():
    if not os.path.exists(os.path.join(ROOT, COSTS)):
        return {}
    return parse_costs(read(COSTS))


def plan_costs(path):
    """The cost table `plan` deals by: the one at `path` (CI_SHARDS_COSTS, which the nightly
    workflow refreshes from recent green runs) when it names a file that parses, else the
    committed one. A refreshed table only moves steps between shards, never drops one."""
    if path and os.path.isfile(path):
        try:
            with open(path, encoding='utf-8') as handle:
                table = parse_costs(handle.read())
            print('ci-shards: costs from %s (%d steps)' % (path, len(table)), file=sys.stderr)
            return table
        except (AssertionError, ValueError, UnicodeDecodeError) as error:
            print('ci-shards: warning: %s does not parse (%s); using %s' % (path, error, COSTS),
                  file=sys.stderr)
    return read_costs()


@functools.cache
def llvm_steps():
    """The test:<package> steps whose binary LLVM compiles: build.zig's `llvm_test_packages`
    and its `optimized_test_packages`, which compile ReleaseSafe."""
    out, text = set(), read('build.zig')
    for name in ('llvm_test_packages', 'optimized_test_packages'):
        match = re.search(NAME_LIST % name, text, re.M | re.S)
        assert match, 'build.zig has no `const %s = [_][]const u8{ ... };`' % name
        rows = re.findall(r'^    "([^"]+)",$', match.group(1), re.M)
        assert rows, '%s: one quoted package per line' % name
        out |= {'test:' + row for row in rows}
    return frozenset(out)


def weighted(costs):
    """{step: (compile, runs)} with each compile priced by its backend (LLVM_COMPILE_WEIGHT or
    SELF_COMPILE_WEIGHT) and runs as they are: what `plan` deals."""
    llvm, out = llvm_steps(), {}
    for name, (compile_seconds, runs) in costs.items():
        weight = LLVM_COMPILE_WEIGHT if name in llvm else SELF_COMPILE_WEIGHT
        out[name] = (compile_seconds * weight, runs)
    assert out.keys() == costs.keys()
    return out


def parse_costs(text):
    """{step: (compile seconds, (run seconds, ...))} of a cost table: a line is the step, its
    compile and its runs, comma-separated, one per shard. A cost that is not a finite number of
    at least zero, or a step listed twice, is an error: a plan must never rest on a cost it
    cannot add up."""
    table = {}
    for number, line in enumerate(text.splitlines(), 1):
        if line and not line.startswith('#'):
            step, compile_seconds, runs = line.split('\t')
            values = [float(compile_seconds)] + [float(run) for run in runs.split(',')]
            assert all(math.isfinite(v) and v >= 0 for v in values), '%s:%d: %s' % (
                COSTS, number, line)
            assert step not in table, '%s:%d: %s is listed twice' % (COSTS, number, step)
            table[step] = (values[0], tuple(values[1:]))
    return table


def total(cost):
    """Seconds of a (compile, runs) cost."""
    return cost[0] + sum(cost[1])


def costs_of(names, table):
    """{step: (compile, runs)}: a step the table lacks costs DEFAULT_SECONDS and is never split;
    a sharded step whose table runs do not match its shard count spreads their sum evenly."""
    counts, out = test_shards(), {}
    for name in names:
        if name not in table:
            print('ci-shards: warning: %s has no cost in %s; using %.0f s' %
                  (name, COSTS, DEFAULT_SECONDS), file=sys.stderr)
        compile_seconds, runs = table.get(name, (DEFAULT_SECONDS, (0.0,)))
        count = counts.get(name, 1)
        if len(runs) != count:
            if name in table:
                print('ci-shards: warning: %s lists %d runs of %s, which runs %d shards' %
                      (COSTS, len(runs), name, count), file=sys.stderr)
            runs = (sum(runs) / count,) * count
        out[name] = (compile_seconds, runs)
    return out


def split(costs, count):
    """The steps dealt to `count` shards, largest cost first, each to the least loaded shard
    (ties: the one with fewer steps, then the lowest index), so no shard is empty while another
    holds two steps."""
    assert count >= 1
    shards, loads = [[] for _ in range(count)], [0.0] * count
    for name in sorted(costs, key=lambda n: (-costs[n], n)):
        index = min(range(count), key=lambda i: (loads[i], len(shards[i]), i))
        shards[index].append(name)
        loads[index] += costs[name]
    assert sum(len(s) for s in shards) == len(costs)
    return shards, loads


def seconds(amount, unit):
    if unit == 'm':
        return amount * 60.0 + 30.0  # the log truncates to whole minutes
    return amount * UNIT_SECONDS[unit]


def binaries():
    """{test binary name: CI step}: a binary is named after its package with `/` as `-`."""
    out = {name.replace('/', '-'): package_step(name, only)
           for name, (_, only) in packages().items()}
    assert len(out) == len(packages()), 'two packages share a test binary name'
    return out


def node_step(text, top, by_binary, known):
    """The CI step a timed node of a summary tree belongs to, or None."""
    match = re.match(r'(?:run|compile) test (\S+)', text)
    if match:
        return by_binary.get(match.group(1))
    if text.startswith('compile exe statetest '):
        return 'test:statetest'
    return top if top in known else None


def log_costs(path, by_binary, known):
    """{step: [compile seconds, {shard: run seconds}]} of one `zig build ... --summary all` log
    (a CI job log works too). A node named `compile ...` is compile time, any other node run
    time, of shard K for `run test <binary> shard K/N` and of shard 0 otherwise."""
    out, top, in_summary = {}, None, False
    with open(path, encoding='utf-8', errors='replace') as handle:
        lines = handle.read().splitlines()
    for raw in lines:
        line = TIMESTAMP.sub('', ANSI.sub('', raw))
        measured = MEASURED.match(line)
        if measured and measured.group(1) in known:
            runs = out.setdefault(measured.group(1), [0.0, {}])[1]
            runs[0] = runs.get(0, 0.0) + float(measured.group(2))
        if line.startswith('Build Summary:'):
            in_summary, top = True, None
            continue
        if not line.strip():
            in_summary = False
        if not in_summary:
            continue
        node = NODE.match(line)
        if not node:
            top = line.split(' ')[0]
            continue
        text, times = node.group(1), DURATION.findall(node.group(1))
        step = node_step(text, top, by_binary, known)
        # A step the restored compile cache answered took no compile and no run: its
        # milliseconds measure the cache, not the step.
        if not times or not step or ' cached' in text:
            continue
        value, row = seconds(int(times[-1][0]), times[-1][1]), out.setdefault(step, [0.0, {}])
        if text.startswith('compile '):
            row[0] += value
            continue
        shard = RUN_SHARD.match(text)
        index = int(shard.group(1)) if shard else 0
        row[1][index] = row[1].get(index, 0.0) + value
    return out


def costs(logs):
    """Rewrite the cost table: a step's compile, and each of its shards' runs, takes the median
    of the logs that measure it; what no log measures keeps its old value."""
    known, by_binary, counts = set(steps()), binaries(), test_shards()
    samples = {}
    for path in logs:
        for step, (compile_seconds, runs) in log_costs(path, by_binary, known).items():
            if compile_seconds:
                samples.setdefault((step, 'compile'), []).append(compile_seconds)
            for index, value in runs.items():
                samples.setdefault((step, index), []).append(value)
    measured = {step for step, _ in samples}
    table = costs_of([s for s in sorted(read_costs()) if s in known], read_costs())
    for step in sorted(measured):
        count = counts.get(step, 1)
        old_compile, old_runs = table.get(step, (0.0, (0.0,) * count))
        def median(key, old):
            return statistics.median(samples[key]) if key in samples else old
        runs = tuple(median((step, index), old_runs[index]) for index in range(count))
        table[step] = (median((step, 'compile'), old_compile), runs)
    write_costs(table)
    print('ci-shards: %d steps measured in %d logs; %d of %d CI steps have no cost' %
          (len(measured), len(logs), len(known - set(table)), len(known)))
    return 0


def write_costs(table):
    lines = ['# CI step, then seconds of its compiles, then of its runs, one per shard',
             '# (etc/scripts/ci-shards.py costs LOG...): durations in `zig build <steps>',
             '# --summary all` logs, summed, the median over logs. No step totals under %.1f s.' %
             MIN_SECONDS]
    for step in sorted(table):
        compile_seconds, runs = table[step]
        runs = [round(run, 1) for run in runs]
        compile_seconds = max(round(compile_seconds, 1), MIN_SECONDS - sum(runs))
        lines.append('%s\t%.1f\t%s' % (step, compile_seconds, ','.join('%.1f' % r for r in runs)))
    with open(os.path.join(ROOT, COSTS), 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(lines) + '\n')


def recipes(text):
    """{name: (header line, body lines without indentation or blank lines)} of a Justfile."""
    out, current = {}, None
    for line in text.splitlines():
        if line.startswith((' ', '\t')):
            if current and line.strip():
                out[current][1].append(line.strip())
            continue
        current = None
        if line and ':' in line and not line.startswith(('#', 'alias ', 'set ', 'mod ')):
            current = re.split(r'[\s:]', line, 1)[0]
            out[current] = (line, [])
    return out


def justfile_errors(text):
    found = recipes(text)
    errors = ['Justfile: recipe %r is not %r; CI mirrors `just ci` (JUST_CI in '
              'etc/scripts/ci-shards.py): change both' % (name, expected)
              for name, expected in sorted(JUST_CI.items()) if found.get(name) != expected]
    settings, modules = [], []
    for number, line in enumerate(text.splitlines(), 1):
        if not line or line.startswith((' ', '\t', '#')):
            continue
        if line.startswith('set '):
            settings.append(line)
        elif line.startswith(('mod ', 'mod? ', 'import ', 'import? ')):
            modules.append(line)
        elif not JUST_TOP_LEVEL.match(line):
            errors.append('Justfile:%d: %r is not a comment, setting, module, alias, '
                          'variable or recipe that check knows' % (number, line))
    if settings != JUST_SETTINGS:
        errors.append('Justfile: settings %r are not JUST_SETTINGS %r' % (settings, JUST_SETTINGS))
    if modules != JUST_MODULES:
        errors.append('Justfile: modules %r are not JUST_MODULES %r' % (modules, JUST_MODULES))
    return errors + missing_modules(modules, ROOT)


def missing_modules(lines, root):
    """An error for each `mod` or `import` line whose quoted file does not exist under root."""
    paths = [(line, line.split("'")[1] if "'" in line else '') for line in lines]
    return ['Justfile: %r names no file' % line for line, path in paths
            if not os.path.isfile(os.path.join(root, path))]


# Justfile changes `check` must catch: (name, text today, text after the change).
JUST_DRIFTS = [
    ('a module naming a missing file', "mod devnet 'etc/devnet/devnet.just'",
     "mod devnet 'etc/devnet/missing.just'"),
    ('an added import', "mod devnet 'etc/devnet/devnet.just'\n",
     "mod devnet 'etc/devnet/devnet.just'\nimport 'etc/devnet/devnet.just'\n"),
    ('an added setting', 'set positional-arguments := true\n',
     "set positional-arguments := true\nset shell := ['false']\n"),
    ('an unknown top-level line', 'alias t := test\n', 'alias t := test\n[unknown]\n'),
]


def selftest():
    """The planner's guarantees on fixed inputs: a path git would quote is read whole, a path
    CI cannot place plans every step, steps that cost nothing still get a shard, a cost that
    is not a finite number of at least zero is rejected, and a drifted `just ci` is caught."""
    errors, rows = [], packages()
    path = 'src/common/rlp/testdata/rlp-\u00e4.json'
    if quoted_diff(path) != [path]:
        errors.append('changed_paths does not read %r whole' % path)
    if changed_packages(['"src/common/rlp/x"'], rows) is not None:
        errors.append('a path outside the known places does not plan every step')
    errors += path_selftest(rows)
    zero = {name: (0.0, (0.0,)) for name in steps()}
    for limit in (1, 12):
        shards = plan_shards(zero, limit)
        if len(shards) != 1 or sorted(shards[0]) != sorted(zero):
            errors.append('steps that cost nothing do not get exactly one shard')
    for bad in ('nan', 'inf', '-1'):
        for line in ('test:x\t%s\t0\n', 'test:x\t0\t1,%s\n'):
            try:
                parse_costs(line % bad)
                errors.append('a cost of %s is accepted' % bad)
            except AssertionError:
                pass
    errors += group_selftest() + cost_selftest()
    added = '    zig build test -Doptimize=ReleaseSafe\n'
    drifted = read('Justfile').replace('ci: tidy test check\n', 'ci: tidy test check\n' + added)
    if not justfile_errors(drifted):
        errors.append('a command added to `ci` is not caught')
    real = read('Justfile')
    for name, old, new in JUST_DRIFTS:
        if old not in real or not justfile_errors(real.replace(old, new, 1)):
            errors.append('%s in the Justfile is not caught' % name)
    with tempfile.TemporaryDirectory() as empty:
        if not missing_modules(JUST_MODULES, empty):
            errors.append('a module whose file is gone is not caught')
    return errors


# (path, what changed_packages returns for it alone): None plans every step.
PATH_CASES = [
    ('docs/TIGER_STYLE.md', (set(), False, False)),
    ('.smithers/factory.json', (set(), False, False)),
    ('.smithers/flows/triage.md', (set(), False, False)),
    ('AGENTS.md', (set(), False, False)),
    ('etc/devnet/devnet.just', (set(), False, False)),
    ('etc/scripts/tidy.sh', (set(), False, False)),
    (COSTS, (set(), False, True)),
    ('etc/ci/shard.sh', (set(), False, True)),
    ('etc/ci/baseline.sh', (set(), False, True)),
    ('etc/ci/README.md', (set(), False, True)),
    ('etc/scripts/ci-shards.py', (set(), False, True)),
    ('etc/scripts/packages.py', (set(), False, True)),
    ('.github/workflows/test.yml', (set(), False, True)),
    ('.github/workflows/nightly.yml', (set(), False, True)),
    ('.github/actions/zig/action.yml', None),
    ('build.zig', None),
    ('build.zig.zon', None),
    ('packages.zon', None),
    ('etc/scripts/install-zig.sh', None),
    ('src/testing/test_runner.zig', None),
    ('Makefile', None),  # a path CI cannot place
    ('src/common/rlp/root.zig', ({'common/rlp'}, False, False)),
]


def path_selftest(rows):
    """Each path class plans what PATH_CASES says: no step for docs/ and .smithers/, CANARY for
    CI's plumbing, every step for the build and the toolchain."""
    errors = []
    for path, expected in PATH_CASES:
        got = changed_packages([path], rows)
        if got != expected:
            errors.append('%s plans %r, not %r' % (path, got, expected))
    if CANARY not in steps() or CANARY in test_shards() or CANARY in llvm_steps():
        errors.append('CANARY %s is not a plain, unsharded, self-hosted step' % CANARY)
    everything = set(steps()) - {'tidy'}
    for paths, expected in [(['docs/x.md', '.smithers/home.json'], set()),
                            ([COSTS, '.github/workflows/test.yml'], {CANARY}),
                            ([COSTS, 'build.zig'], everything)]:
        if affected_paths(paths) != expected:
            errors.append('%s plans %s, not %s' % (paths, sorted(affected_paths(paths)),
                                                   sorted(expected)))
    return errors


def cost_selftest():
    """Compiles are priced by backend, a cached node is not measured, and plan_costs falls back
    to the committed table for a missing or broken refreshed one."""
    errors, llvm = [], sorted(llvm_steps())
    if 'test:testing/sim' not in llvm or 'test:infra/tx_manager' not in llvm:
        errors.append('llvm_steps misses build.zig\'s LLVM or ReleaseSafe test packages')
    priced = weighted({llvm[0]: (10.0, (1.0,)), CANARY: (10.0, (1.0,))})
    if priced != {llvm[0]: (10.0 * LLVM_COMPILE_WEIGHT, (1.0,)),
                  CANARY: (10.0 * SELF_COMPILE_WEIGHT, (1.0,))}:
        errors.append('weighted does not price a compile by its backend: %r' % priced)
    lines = ['Build Summary: 2/2 steps succeeded', 'test:common/rlp cached',
             '+- run test common-rlp cached',
             '   +- compile test common-rlp debug native cached 76ms MaxRSS:31M', '',
             'Build Summary: 2/2 steps succeeded', 'test:common/rlp success',
             '+- run test common-rlp 14 pass (14 total) 3s MaxRSS:2M',
             '   +- compile test common-rlp debug native success 2s MaxRSS:31M', '']
    with tempfile.TemporaryDirectory() as root:
        log, broken = os.path.join(root, 'job.log'), os.path.join(root, 'costs.tsv')
        with open(log, 'w', encoding='utf-8') as handle:
            handle.write('\n'.join(lines))
        measured = log_costs(log, binaries(), set(steps()))
        if measured != {CANARY: [2.0, {0: 3.0}]}:
            errors.append('a cached step is measured: %r' % measured)
        with open(broken, 'w', encoding='utf-8') as handle:
            handle.write('test:x\tnan\t0\n')
        with contextlib.redirect_stderr(io.StringIO()):
            for path in (broken, os.path.join(root, 'missing.tsv'), None):
                if plan_costs(path) != read_costs():
                    errors.append('plan_costs(%r) is not the committed table' % path)
    return errors


def group_selftest():
    """The dealer's guarantees for groups: a matrix that skips a group, runs one twice, or runs
    a step beside its group is caught; plan never splits a step into groups, even one whose
    runs fill several shards; a step whose chain fills a shard gets one to itself."""
    errors, counts = [], {'test:x': 4, 'tidy': 1}
    whole = ['test:x@1/2', 'test:x@2/2', 'tidy']
    if coverage_errors(whole, counts):
        errors.append('the groups of one G are not taken to run every shard once')
    for name, names in [('a skipped group', whole[1:]), ('a group run twice', whole + whole[:1]),
                        ('a step beside its group', whole + ['test:x']),
                        ('a group build.zig lacks', ['test:x@1/1', 'tidy'])]:
        if not coverage_errors(names, counts):
            errors.append('%s is not caught' % name)
    for runs in [(SHARD_SECONDS,) * 4, (SHARD_SECONDS * 4.0, 0.0, 0.0, 0.0)]:
        dealt = plan_shards({'test:x': (2.0, runs), 'tidy': (0.0, (2.0,))}, 16)
        if sorted(name for shard in dealt for name in shard) != ['test:x', 'tidy']:
            errors.append('a step whose runs fill shards is split: %r' % dealt)
    chain = SHARD_SECONDS / CHAIN_WEIGHT
    long = {'test:long': (0.0, (chain,))}
    long.update({'test:%d' % i: (0.0, (chain / 3.0,)) for i in range(5)})
    if ['test:long'] not in plan_shards(long, 12):
        errors.append('a step whose one run fills a shard does not get a shard to itself')
    return errors + selection_selftest() + listing_selftest()


def selection_selftest():
    """Partial selections of the real steps, and steps the cost table lacks, are planned to run
    each shard of each selected step once, at every limit."""
    errors, everything = [], steps()
    with contextlib.redirect_stderr(io.StringIO()):  # the missing rows' warnings
        table = read_costs()
        missing = {step: table[step] for step in table if step != 'test:common/crypto'}
        selections = [(everything[::k], table) for k in (2, 3, 7)]
        selections += [(sorted(test_shards()), table), (everything, missing),
                       (['test:common/crypto', 'test:no-row'], {})]
        for names, rows in selections:
            costs = costs_of(names, rows)
            counts = {name: test_shards().get(name, 1) for name in names}
            for limit in range(1, MAX_SHARDS + 1):
                planned = [name for shard in plan_shards(costs, limit) for name in shard]
                if coverage_errors(planned, counts):
                    errors.append('a selection of %d steps (%d without a cost row) at limit %d'
                                  ' is not planned once each' % (
                                      len(names), len(set(names) - set(rows)), limit))
                    break
    return errors


def listing(ranges):
    """`zig build -l` lines of groups {name: (first, last, count)}, as build.zig writes them."""
    return ''.join('  %-28s Run test shards %d to %d of %d of the %s package\n' % (
        name, first, last, count, GROUP.match(name).group(1)[len('test:'):])
        for name, (first, last, count) in sorted(ranges.items()))


def priced_groups():
    """{group: (first shard, last shard, count)} of every group group_shards prices."""
    out = {}
    for step, count in sorted(test_shards().items()):
        for groups in [g for g in group_counts() if g <= count]:
            for group in range(1, groups + 1):
                shards = group_shards(group, groups, count)
                out[group_name(step, group, groups)] = (shards.start, shards.stop - 1, count)
    return out


def listing_errors(text):
    """Why the groups that `zig build -l` lists in `text` are not the ones group_shards prices,
    with the same shards, or []."""
    errors, listed = [], {}
    for line in text.splitlines():
        if not re.match(r'  test:\S+@\d', line):
            continue
        match = LISTED_GROUP.match(line)
        if not match or match.group(2) != match.group(6):
            errors.append('zig build -l: %r is not a group as add_test_groups lists it' % line)
            continue
        listed[match.group(1)] = tuple(int(match.group(n)) for n in (3, 4, 5))
    expected = priced_groups()
    for name in sorted(set(expected) | set(listed)):
        if listed.get(name) != expected.get(name):
            errors.append('%s: build.zig runs shards %s, ci-shards.py prices %s' % (
                name, listed.get(name, 'none'), expected.get(name, 'none')))
    return errors


def listing_selftest():
    """listing_errors accepts build.zig's groups and catches swapped, missing and extra ones."""
    errors, ranges = [], priced_groups()
    if listing_errors('  test:x                       Run tests\n' + listing(ranges)):
        errors.append('the groups group_shards prices are not accepted as listed')
    swapped = dict(ranges)
    first, second = 'test:proof/tee@1/2', 'test:proof/tee@2/2'
    swapped[first], swapped[second] = ranges[second], ranges[first]
    extra = dict(ranges, **{'test:proof/tee@5/5': (0, 11, 12)})
    for name, changed in [('swapped groups', swapped), ('an extra group', extra),
                          ('a missing group', {k: v for k, v in ranges.items() if k != first})]:
        if not listing_errors(listing(changed)):
            errors.append('%s in `zig build -l` is not caught' % name)
    return errors


def quoted_diff(path):
    """changed_paths between two trees, in a scratch repository, that differ in one file."""
    with tempfile.TemporaryDirectory() as root:
        def git(*args, data=None):
            done = subprocess.run(['git', '-C', root] + list(args), input=data,
                                  capture_output=True, check=True)
            return done.stdout.decode().strip()
        git('init', '-q')
        trees = []
        for body in (b'one', b'two'):
            blob = git('hash-object', '-w', '--stdin', data=body)
            git('update-index', '--add', '--cacheinfo', '100644,%s,%s' % (blob, path))
            trees.append(git('write-tree'))
        return changed_paths(trees[0], trees[1], root)


def check():
    """Every package has one CI step, ci_steps are well formed, and for every shard count the
    shards' union is every CI step, each exactly once."""
    errors, everything = ['selftest: ' + e for e in selftest()], steps()
    errors += justfile_errors(read('Justfile'))
    for name in ci_steps():
        if not re.match(r'(test|check):[a-z][a-z0-9-]*$', name):
            errors.append('ci_steps: %r is not test:<name> or check:<name>' % name)
    if sorted(TOOLS) != sorted(ci_steps()) or not set(TOOLS.values()) <= set(packages()):
        errors.append('TOOLS must name each of build.zig\'s ci_steps and the package it imports')
    if sorted(affected(None, 'HEAD') | {'tidy'}) != everything:
        errors.append('plan: an unknown base does not plan every step but tidy')
    table = read_costs()
    stale = sorted(set(table) - set(everything))
    if stale:
        print('ci-shards: warning: %s lists steps CI no longer runs: %s' %
              (COSTS, ' '.join(stale)), file=sys.stderr)
    costs = costs_of(everything, table)
    whole = {name: total(cost) for name, cost in costs.items()}
    for count in range(1, MAX_SHARDS + 1):
        dealt = [step for shard in split(whole, count)[0] for step in shard]
        if sorted(dealt) != everything:
            errors.append('%d shards: steps skipped or dealt twice' % count)
    errors += group_errors(costs)
    errors += fuzz_errors()
    for e in errors:
        print('ci-shards: ' + e)
    return 1 if errors else 0


def group_errors(costs):
    """The groups of each G up to a binary's shard count run each shard once, for every count
    up to MAX_SHARDS; and for every limit, plan's shards run each shard of every step once."""
    errors, counts = [], {step: len(runs) for step, (_, runs) in costs.items()}
    for count in range(2, MAX_SHARDS + 1):
        for groups in [g for g in group_counts() if g <= count]:
            dealt = [s for k in range(1, groups + 1) for s in group_shards(k, groups, count)]
            if dealt != list(range(count)):
                errors.append('%d groups of %d shards do not hold each shard once' % (
                    groups, count))
    for limit in range(1, MAX_SHARDS + 1):
        names = [name for shard in plan_shards(costs, limit) for name in shard]
        errors += ['limit %d: %s' % (limit, e) for e in coverage_errors(names, counts)]
    return errors


def shard(index, count):
    assert 0 <= index < count <= MAX_SHARDS, 'shard INDEX COUNT: 0 <= INDEX < COUNT <= 64'
    costs = {name: total(cost) for name, cost in costs_of(steps(), read_costs()).items()}
    shards, loads = split(costs, count)
    print('ci-shards: shard %d/%d: %d steps, %.0f s of %.0f s' %
          (index, count, len(shards[index]), loads[index], sum(loads)), file=sys.stderr)
    print('\n'.join(shards[index]))
    return 0


FUZZ_TARGET = re.compile(r'\b(?:testing\.fuzz|fuzz\.run|fuzz\.run_bytes)\(')
FUZZ_TEST = re.compile(r'^test "[^"]*fuzz', re.M | re.I)
# Inputs per coverage-guided target (`--fuzz=RUNS` counts each target's inputs). 20000 is more
# than a soak's 50 x 256 seeded inputs; the slowest binaries take under 300 inputs a second on
# a 4-processor box, so fewer keeps their fuzz phase near 20 minutes.
FUZZ_RUNS_DEFAULT = 20000
FUZZ_RUNS = {'test:execution/node': 2000, 'test:execution/rpc': 5000,
             'test:execution/db': 5000}


def fuzz_weights():
    """({test:<package>: ReleaseSafe compile seconds}, the steps with a coverage-guided target)
    of each package with a fuzz target or a test named for fuzzing (comment lines aside). A
    step FUZZ_COSTS lacks costs DEFAULT_SECONDS. Tool files belong to no package's test binary."""
    rules, _ = package_rules()
    texts, tools, _ = rules.read_sources()
    rows, targets, seeded = packages(), set(), set()
    for path, text in texts.items():
        if path in tools:
            continue
        code = rules.COMMENT.sub('', text)
        if FUZZ_TARGET.search(code):
            targets.add(rules.package_of(path))
        elif FUZZ_TEST.search(code):
            seeded.add(rules.package_of(path))
    names = targets | seeded
    assert names <= set(rows), 'fuzz: packages.zon lacks %s' % sorted(names - set(rows))
    table = read_fuzz_costs()
    out = {'test:' + name: table.get('test:' + name, DEFAULT_SECONDS) for name in names}
    return out, {'test:' + name for name in targets}


def read_fuzz_costs():
    """{step: ReleaseSafe compile seconds} of FUZZ_COSTS: lines `STEP<TAB>SECONDS`."""
    out = {}
    for line in read(FUZZ_COSTS).splitlines():
        if not line or line.startswith('#'):
            continue
        step, value = line.split('\t')
        seconds_value = float(value)
        assert math.isfinite(seconds_value) and seconds_value >= 0, 'fuzz-costs: bad %r' % line
        out[step] = seconds_value
    return out


def fuzz_costs(logs):
    """Rewrite FUZZ_COSTS: each step's compile seconds, the median over the logs that measure
    it; a step no log measures keeps its old value."""
    known, by_binary, samples = set(steps()), binaries(), {}
    for path in logs:
        for step, (compile_seconds, _) in log_costs(path, by_binary, known).items():
            if compile_seconds:
                samples.setdefault(step, []).append(compile_seconds)
    table = {step: value for step, value in read_fuzz_costs().items() if step in known}
    table.update({step: statistics.median(values) for step, values in samples.items()})
    lines = ['# CI step, then the seconds of its ReleaseSafe test-binary compile, the median over',
             '# `zig build ... -Doptimize=ReleaseSafe --summary all` logs (ci-shards.py fuzz-costs',
             '# LOG...). The nightly fuzz groups are dealt by them (ci-shards.py fuzz).']
    lines += ['%s\t%.0f' % (step, table[step]) for step in sorted(table)]
    with open(os.path.join(ROOT, FUZZ_COSTS), 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(lines) + '\n')
    print('ci-shards: %d steps measured in %d logs' % (len(samples), len(logs)))
    return 0


def fuzz_lines(group, targets):
    """A group's `RUNS STEP...` lines (fewest runs first) and its `soak STEP...` line."""
    by_runs = collections.defaultdict(list)
    for step in sorted(group):
        by_runs[FUZZ_RUNS.get(step, FUZZ_RUNS_DEFAULT) if step in targets else 'soak'].append(step)
    keys = sorted((k for k in by_runs if k != 'soak')) + (['soak'] if 'soak' in by_runs else [])
    return ['%s %s' % (key, ' '.join(by_runs[key])) for key in keys]


def fuzz_group(index, count):
    assert 0 <= index < count <= MAX_SHARDS, 'fuzz INDEX COUNT: 0 <= INDEX < COUNT <= 64'
    weights, targets = fuzz_weights()
    groups, loads = split(weights, count)
    print('ci-shards: fuzz %d/%d: %d of %d steps, %.0f of %.0f s of ReleaseSafe compiles' %
          (index, count, len(groups[index]), len(weights), loads[index], sum(loads)),
          file=sys.stderr)
    print('\n'.join(fuzz_lines(groups[index], targets)))
    return 0


def fuzz_errors():
    """Every fuzz step is a CI test step, FUZZ_RUNS names fuzz targets, the list is not empty,
    and for every count the groups hold each fuzz step once and none is empty while another
    holds two; each group's lines name each of its steps once."""
    errors, everything = [], set(steps())
    weights, targets = fuzz_weights()
    if not targets:
        errors.append('fuzz: no package holds a coverage-guided fuzz target')
    unknown = sorted(set(weights) - everything)
    if unknown:
        errors.append('fuzz: not CI test steps: %s' % ' '.join(unknown))
    stale = sorted(set(FUZZ_RUNS) - targets)
    if stale:
        errors.append('fuzz: FUZZ_RUNS names steps without a fuzz target: %s' % ' '.join(stale))
    for count in range(1, min(MAX_SHARDS, len(weights)) + 1):
        groups, _ = split(weights, count)
        dealt = [step for group in groups for step in group]
        if sorted(dealt) != sorted(weights) or not all(groups):
            errors.append('fuzz: %d groups skip, repeat or leave out a step' % count)
        listed = [s for g in groups for line in fuzz_lines(g, targets) for s in line.split()[1:]]
        if sorted(listed) != sorted(weights):
            errors.append('fuzz: %d groups\' lines skip or repeat a step' % count)
    return errors


def package_rules():
    """etc/scripts/packages.py as a module, its sources read: package_of() and the tool files."""
    sys.dont_write_bytecode = True  # no __pycache__ beside the scripts
    spec = importlib.util.spec_from_file_location(
        'packages', os.path.join(ROOT, 'etc/scripts/packages.py'))
    rules = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rules)
    rules.ROOT = ROOT
    _, tools, _ = rules.read_sources()
    return rules, tools


def changed_paths(base, head, root=ROOT):
    """Paths that differ between two trees (or commits), or None when the base is unknown.
    NUL-separated (`-z`): git quotes a name with a tab, a quote or a non-ASCII byte otherwise."""
    if not base or set(base) == {'0'}:
        return None
    diff = subprocess.run(['git', '-C', root, 'diff', '--name-only', '--no-renames', '-z', base,
                           head], capture_output=True)
    if diff.returncode != 0:
        return None
    return [p for p in diff.stdout.decode('utf-8', 'surrogateescape').split('\0') if p]


def changed_packages(paths, rows):
    """(packages whose files changed, whether a tool's own file changed, whether CI's plumbing
    changed), or None: rerun all."""
    rules, tools = package_rules()
    names, tool_files, plumbing = set(), False, False
    for path in paths:
        if path in EVERYTHING or path.startswith(EVERYTHING_DIRECTORIES):
            return None
        if path in CI_FILES or path.startswith(CI_DIRECTORIES):
            plumbing = True
            continue
        if path in NO_STEP_FILES or path.startswith(NO_STEP_DIRECTORIES):
            continue
        if not path.startswith(('src/', 'bin/')):
            return None  # a path CI cannot place: plan every step, never none
        if path in tools:
            tool_files = True
            continue
        try:
            name = rules.package_of(path)
        except AssertionError:
            return None
        if name not in rows:
            return None
        names.add(name)
    return names, tool_files, plumbing


def importers(names, rows):
    """`names` and every package that imports one of them, directly or not."""
    dependents = {}
    for name, (deps, _) in rows.items():
        for dep in deps:
            dependents.setdefault(dep, set()).add(name)
    seen, stack = set(names), list(names)
    while stack:  # bounded: each package enters `seen` once
        for parent in dependents.get(stack.pop(), ()):
            if parent not in seen:
                seen.add(parent)
                stack.append(parent)
    return seen


def affected(base, head):
    """The Zig steps a push from `base` to `head` must run (tidy runs on every push)."""
    return affected_paths(changed_paths(base, head))


def affected_paths(paths):
    """The Zig steps a change to `paths` must run; every one when `paths` is None."""
    rows, everything = packages(), set(steps()) - {'tidy'}
    changed = None if paths is None else changed_packages(paths, rows)
    if changed is None:
        return everything
    names, tool_files, plumbing = changed
    closure = importers(names, rows)
    out = {package_step(name, rows[name][1]) for name in closure}
    out |= {step for step, package in TOOLS.items() if tool_files or package in closure}
    if plumbing:
        out.add(CANARY)
    assert out <= everything
    return out


def part_cost(name, costs):
    """Seconds of a step, or of a group `<step>@K/G`: the compile plus its shards' runs."""
    group = GROUP.match(name)
    if not group:
        return total(costs[name])
    compile_seconds, runs = costs[group.group(1)]
    shards = group_shards(int(group.group(2)), int(group.group(3)), len(runs))
    return compile_seconds + sum(runs[index] for index in shards)


def part_chain(name, costs):
    """Seconds of a part's compile plus its slowest run: the least its job can take."""
    group = GROUP.match(name)
    compile_seconds, runs = costs[group.group(1) if group else name]
    shards = group_shards(int(group.group(2)), int(group.group(3)), len(runs)) if group else \
        range(len(runs))
    return compile_seconds + max(runs[index] for index in shards)


def deal_weights(parts, costs):
    """{part: weight}: its cost, or CHAIN_WEIGHT times its chain when that fills a shard."""
    out = {}
    for name, cost in parts.items():
        chain = CHAIN_WEIGHT * part_chain(name, costs)
        out[name] = max(cost, chain) if chain >= SHARD_SECONDS else cost
    assert all(out[name] >= parts[name] for name in parts)
    return out


def coverage_errors(names, counts):
    """Why `names` (steps and groups `<step>@K/G`) do not run each shard of each step in
    `counts` ({step: shard count, 1 for a step that does not shard}) exactly once, or []."""
    errors, times = [], {}
    for name in names:
        group = GROUP.match(name)
        step = group.group(1) if group else name
        if step not in counts:
            errors.append('%s is not a planned step' % name)
            continue
        shards = range(counts[step])
        if group:
            index, groups = int(group.group(2)), int(group.group(3))
            if groups not in group_counts() or not 1 <= index <= groups <= counts[step]:
                errors.append('%s is not a group build.zig declares' % name)
                continue
            shards = group_shards(index, groups, counts[step])
        for shard in shards:
            times[(step, shard)] = times.get((step, shard), 0) + 1
    for step, count in sorted(counts.items()):
        errors += ['%s shard %d of %d runs %d times, not once' % (step, shard, count, ran)
                   for shard in range(count) for ran in [times.get((step, shard), 0)]
                   if ran != 1]
    return errors


def plan_shards(costs, limit):
    """The steps of `costs` ({step: (compile, runs)}), whole, dealt to as many shards as they
    fill at SHARD_SECONDS each: at least one when there is a step, at most `limit` and at most
    one per step; none is empty. A step is never split into groups: a job compiles each binary
    once and runs its shards on all its processors."""
    assert 1 <= limit <= MAX_SHARDS
    if not costs:
        return []
    parts = deal_weights({name: total(cost) for name, cost in costs.items()}, costs)
    count = math.ceil(sum(parts.values()) / SHARD_SECONDS)
    shards = split(parts, max(1, min(limit, len(parts), count)))[0]
    assert all(shards) and sum(len(shard) for shard in shards) == len(parts)
    assert not any(GROUP.match(name) for shard in shards for name in shard)
    return shards


def plan(base, head, limit):
    """GitHub outputs: `count`, and `matrix`: tidy and the affected steps, priced by `weighted`
    and dealt by plan_shards. Tidy makes every plan at least one shard, so a push runs
    max(1, count) jobs: the plan job runs a plan of one itself."""
    return plan_steps(affected(base, head), limit)


def plan_steps(affected_steps, limit):
    """`plan`'s outputs for the Zig steps `affected_steps`."""
    selected = sorted(set(affected_steps) | {'tidy'})
    costs = weighted(costs_of(selected, plan_costs(os.environ.get('CI_SHARDS_COSTS'))))
    shards = plan_shards(costs, limit)
    include = [{'shard': i, 'steps': ' '.join(s)} for i, s in enumerate(shards)]
    printed = [step for row in include for step in row['steps'].split(' ')]
    counts = {step: len(runs) for step, (_, runs) in costs.items()}
    assert sorted(counts) == selected and not coverage_errors(printed, counts), \
        'the matrix must run each shard of each selected step exactly once'
    print('ci-shards: plan: %d steps, %.0f weighted s, %d shards' % (
        len(costs), sum(total(cost) for cost in costs.values()), len(shards)), file=sys.stderr)
    for index, row in enumerate(shards):
        print('ci-shards: shard %d: %.0f s: %s' % (
            index, sum(part_cost(name, costs) for name in row), ' '.join(row)), file=sys.stderr)
    print('count=%d' % len(shards))
    print('matrix=%s' % json.dumps({'include': include}, separators=(',', ':')))
    return 0


def main(args):
    if args[:1] == ['steps'] and len(args) == 1:
        print('\n'.join(steps()))
        return 0
    if args[:1] == ['shard'] and len(args) == 3:
        return shard(int(args[1]), int(args[2]))
    if args[:1] == ['costs'] and len(args) >= 2:
        return costs(args[1:])
    if args[:1] == ['plan'] and len(args) == 4:
        return plan(args[1], args[2], int(args[3]))
    if args[:1] == ['preview'] and len(args) >= 2:
        return plan_steps(affected_paths(args[2:] if args[2:] != ['-'] else None), int(args[1]))
    if args == ['check']:
        return check()
    if args[:1] == ['fuzz'] and len(args) == 3:
        return fuzz_group(int(args[1]), int(args[2]))
    if args[:1] == ['fuzz-costs'] and len(args) >= 2:
        return fuzz_costs(args[1:])
    if args[:1] == ['groups'] and len(args) == 2:
        errors = listing_errors(read(args[1]))
        for e in errors:
            print('ci-shards: ' + e)
        print('ci-shards: groups: %d listed groups checked against group_shards' %
              len(priced_groups()), file=sys.stderr)
        return 1 if errors else 0
    sys.exit(__doc__)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
