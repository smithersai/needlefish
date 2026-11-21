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

Costs: etc/ci/costs.tsv, seconds per step: the durations of a step's compiles and runs in a
`zig build <steps> --summary all` log, summed (`costs`). The log prints whole seconds, and
minutes for a step of a minute or more (read as the minute plus 30 s). A step missing from
the table costs DEFAULT_SECONDS, with a warning; it is never dropped.

`shard` deals the steps to COUNT shards: largest cost first, each to the shard with the least
cost so far (ties: the lowest index), so every job computes the same split.

`plan` keeps the steps a push needs: the packages whose files changed between BASE and HEAD,
every package that imports one of them (packages.zon), and the tools that import one. It deals
them to as many shards as they fill at SHARD_SECONDS each, at most LIMIT, and prints them as
GitHub outputs. BASE is the tree of the last commit CI passed (etc/ci/baseline.sh). An unknown
BASE, a change to a file in EVERYTHING, or a path the lists below cannot place plans every step;
a path in NO_STEP_FILES or NO_STEP_DIRECTORIES plans none. Tidy is not planned: it runs on
every push, in its own job.

usage: python3 -I etc/scripts/ci-shards.py steps              every CI step, one per line
       python3 -I etc/scripts/ci-shards.py shard INDEX COUNT  shard INDEX's steps (0-based)
       python3 -I etc/scripts/ci-shards.py plan BASE HEAD LIMIT   count= and matrix= outputs
       python3 -I etc/scripts/ci-shards.py costs LOG...       rewrite etc/ci/costs.tsv from logs
       python3 -I etc/scripts/ci-shards.py check              the shards cover each step once
"""
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
DEFAULT_SECONDS = 10.0  # a step the table does not list yet
MIN_SECONDS = 0.1  # the least a measured step costs in the table, so none rounds to 0.0
MAX_SHARDS = 64  # `check` verifies every count up to this one
# The cost `plan` puts in one shard before it starts another. On a 4-vCPU runner a shard's
# work took about half its summed step durations, so this is about 30 s of work: with the
# shard's setup it stays within the one minute GitHub bills a job at least.
SHARD_SECONDS = 60.0
CI_STEPS = re.compile(r'^const ci_steps = \[_\]\[\]const u8\{\n(.*?)^\};', re.M | re.S)
NAME = re.compile(r'\.name = "([^"]+)"')
DURATION = re.compile(r' (\d+)(ns|us|ms|s|m)(?= |$)')
TIMESTAMP = re.compile(r'^\d{4}-\d\d-\d\dT[\d:.]+Z ')
ANSI = re.compile(r'\x1b\[[0-9;]*m')
NODE = re.compile(r'^(?:\|  |   )*\+- (.*)$')
MEASURED = re.compile(r'^ci-shards: (\S+) (\d+)s$')
UNIT_SECONDS = {'ns': 1e-9, 'us': 1e-6, 'ms': 1e-3, 's': 1.0}
# Paths whose change reruns every step: what the build, the test runner, the toolchain and
# this plan are made of.
EVERYTHING = {'build.zig', 'build.zig.zon', 'packages.zon', COSTS, 'etc/ci/shard.sh',
              'etc/ci/baseline.sh',
              'etc/scripts/ci-shards.py', 'etc/scripts/packages.py', 'etc/scripts/install-zig.sh',
              'src/testing/test_runner.zig'}
EVERYTHING_DIRECTORIES = ('.github/',)
# Paths no step reads: a change to one plans no step (tidy, which reads etc/scripts/, runs on
# every push and checks the Justfile). A path in none of these lists, outside src/ and bin/,
# plans every step.
NO_STEP_FILES = {'.gitignore', 'AGENTS.md', 'CLAUDE.md', 'Justfile', 'LICENSE', 'README.md'}
NO_STEP_DIRECTORIES = ('docs/', 'etc/')
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


def read_costs():
    if not os.path.exists(os.path.join(ROOT, COSTS)):
        return {}
    return parse_costs(read(COSTS))


def parse_costs(text):
    """{step: seconds} of a cost table. A cost that is not a finite number of at least zero, or a
    step listed twice, is an error: a plan must never rest on a cost it cannot add up."""
    table = {}
    for number, line in enumerate(text.splitlines(), 1):
        if line and not line.startswith('#'):
            step, seconds = line.split('\t')
            value = float(seconds)
            assert math.isfinite(value) and value >= 0, '%s:%d: %s' % (COSTS, number, line)
            assert step not in table, '%s:%d: %s is listed twice' % (COSTS, number, step)
            table[step] = value
    return table


def costs_of(names, table):
    out = {}
    for name in names:
        if name not in table:
            print('ci-shards: warning: %s has no cost in %s; using %.0f s' %
                  (name, COSTS, DEFAULT_SECONDS), file=sys.stderr)
        out[name] = table.get(name, DEFAULT_SECONDS)
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
    """{step: seconds} of one `zig build ... --summary all` log (a CI job log works too)."""
    out, top, in_summary = {}, None, False
    with open(path, encoding='utf-8', errors='replace') as handle:
        lines = handle.read().splitlines()
    for raw in lines:
        line = TIMESTAMP.sub('', ANSI.sub('', raw))
        measured = MEASURED.match(line)
        if measured and measured.group(1) in known:
            out[measured.group(1)] = out.get(measured.group(1), 0.0) + float(measured.group(2))
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
        times = DURATION.findall(node.group(1))
        step = node_step(node.group(1), top, by_binary, known)
        if times and step:
            out[step] = out.get(step, 0.0) + seconds(int(times[-1][0]), times[-1][1])
    return out


def costs(logs):
    """Rewrite the cost table: each step a log measures takes the median of those logs."""
    known, by_binary = set(steps()), binaries()
    samples = {}
    for path in logs:
        for step, value in log_costs(path, by_binary, known).items():
            samples.setdefault(step, []).append(value)
    table = read_costs()
    table.update({step: statistics.median(values) for step, values in samples.items()})
    lines = ['# CI step -> seconds (etc/scripts/ci-shards.py costs LOG...): the durations of',
             '# its compiles and runs in `zig build <steps> --summary all` logs, summed.']
    lines += ['%s\t%.1f' % (step, max(table[step], MIN_SECONDS)) for step in sorted(table)]
    with open(os.path.join(ROOT, COSTS), 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(lines) + '\n')
    missing = known - set(table)
    print('ci-shards: %d steps measured in %d logs; %d of %d CI steps have no cost' %
          (len(samples), len(logs), len(missing), len(known)))
    return 0


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
    if changed_packages(['docs/TIGER_STYLE.md'], rows) != (set(), False):
        errors.append('a docs/ path plans a step')
    zero = {name: 0.0 for name in steps()}
    for limit in (1, 12):
        shards = plan_shards(zero, limit)
        if len(shards) != 1 or sorted(shards[0]) != sorted(zero):
            errors.append('steps that cost nothing do not get exactly one shard')
    for bad in ('nan', 'inf', '-1'):
        try:
            parse_costs('test:x\t%s\n' % bad)
            errors.append('a cost of %s is accepted' % bad)
        except AssertionError:
            pass
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
    costs = {name: table.get(name, DEFAULT_SECONDS) for name in everything}
    for count in range(1, MAX_SHARDS + 1):
        dealt = [step for shard in split(costs, count)[0] for step in shard]
        if sorted(dealt) != everything:
            errors.append('%d shards: steps skipped or dealt twice' % count)
    for e in errors:
        print('ci-shards: ' + e)
    return 1 if errors else 0


def shard(index, count):
    assert 0 <= index < count <= MAX_SHARDS, 'shard INDEX COUNT: 0 <= INDEX < COUNT <= 64'
    costs = costs_of(steps(), read_costs())
    shards, loads = split(costs, count)
    print('ci-shards: shard %d/%d: %d steps, %.0f s of %.0f s' %
          (index, count, len(shards[index]), loads[index], sum(loads)), file=sys.stderr)
    print('\n'.join(shards[index]))
    return 0


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
    """(packages whose files changed, whether a tool's own file changed), or None: rerun all."""
    rules, tools = package_rules()
    names, tool_files = set(), False
    for path in paths:
        if path in EVERYTHING or path.startswith(EVERYTHING_DIRECTORIES):
            return None
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
    return names, tool_files


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
    rows, everything = packages(), set(steps()) - {'tidy'}
    paths = changed_paths(base, head)
    changed = None if paths is None else changed_packages(paths, rows)
    if changed is None:
        return everything
    names, tool_files = changed
    closure = importers(names, rows)
    out = {package_step(name, rows[name][1]) for name in closure}
    out |= {step for step, package in TOOLS.items() if tool_files or package in closure}
    assert out <= everything
    return out


def plan_shards(costs, limit):
    """The steps of `costs` dealt to as many shards as they fill at SHARD_SECONDS each: at least
    one when there is a step, at most `limit` and at most one per step; none is empty."""
    assert 1 <= limit <= MAX_SHARDS
    if not costs:
        return []
    count = math.ceil(sum(costs.values()) / SHARD_SECONDS)
    shards = split(costs, max(1, min(limit, len(costs), count)))[0]
    assert all(shards) and sorted(s for shard in shards for s in shard) == sorted(costs)
    return shards


def plan(base, head, limit):
    """GitHub outputs: `count`, and `matrix`: the affected steps dealt by plan_shards."""
    selected = sorted(affected(base, head))
    costs = costs_of(selected, read_costs())
    shards = plan_shards(costs, limit)
    include = [{'shard': i, 'steps': ' '.join(s)} for i, s in enumerate(shards)]
    printed = [step for row in include for step in row['steps'].split(' ')]
    assert sorted(printed) == selected, 'the matrix must hold each selected step exactly once'
    print('ci-shards: plan: %d steps, %.0f s, %d shards' % (len(costs), sum(costs.values()),
          len(shards)), file=sys.stderr)
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
    if args == ['check']:
        return check()
    sys.exit(__doc__)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
