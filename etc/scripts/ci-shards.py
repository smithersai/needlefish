#!/usr/bin/env python3
"""CI shards: `just ci` as named steps, dealt to parallel jobs by measured cost.

`just ci` is tidy, `zig build test` and `zig build check`. build.zig makes `test` and `check`
depend on named steps only (its `ci_steps` and `assert_named_dependencies`), so CI runs:
  tidy                 etc/scripts/tidy.sh
  test:<package>       each package `zig build test` runs (packages.zon, not compile_only); it
                       compiles the package's test binary, so it also covers check:<package>
  check:<package>      each compile_only package: compiles its test binary
  ci_steps             build.zig's list: test:statetest and the check:<tool> steps

Costs: etc/ci/costs.tsv, seconds per step: the durations of a step's compiles and runs in a
`zig build <steps> --summary all` log, summed (`costs`). The log prints whole seconds, and
minutes for a step of a minute or more (read as the minute plus 30 s). A step missing from
the table costs DEFAULT_SECONDS, with a warning; it is never dropped.

`shard` deals the steps to COUNT shards: largest cost first, each to the shard with the least
cost so far (ties: the lowest index), so every job computes the same split.

usage: python3 -I etc/scripts/ci-shards.py steps              every CI step, one per line
       python3 -I etc/scripts/ci-shards.py shard INDEX COUNT  shard INDEX's steps (0-based)
       python3 -I etc/scripts/ci-shards.py costs LOG...       rewrite etc/ci/costs.tsv from logs
       python3 -I etc/scripts/ci-shards.py check              the shards cover each step once
"""
import math
import os
import re
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COSTS = 'etc/ci/costs.tsv'
DEFAULT_SECONDS = 10.0  # a step the table does not list yet
MIN_SECONDS = 0.1  # the least a measured step costs in the table, so none rounds to 0.0
MAX_SHARDS = 64  # `check` verifies every count up to this one
CI_STEPS = re.compile(r'^const ci_steps = \[_\]\[\]const u8\{\n(.*?)^\};', re.M | re.S)
NAME = re.compile(r'\.name = "([^"]+)"')
DURATION = re.compile(r' (\d+)(ns|us|ms|s|m)(?= |$)')
TIMESTAMP = re.compile(r'^\d{4}-\d\d-\d\dT[\d:.]+Z ')
ANSI = re.compile(r'\x1b\[[0-9;]*m')
NODE = re.compile(r'^(?:\|  |   )*\+- (.*)$')
MEASURED = re.compile(r'^ci-shards: (\S+) (\d+)s$')
UNIT_SECONDS = {'ns': 1e-9, 'us': 1e-6, 'ms': 1e-3, 's': 1.0}


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


def selftest():
    """The dealer's guarantees on fixed inputs: steps that cost nothing still spread over the
    shards, and a cost that is not a finite number of at least zero is rejected."""
    errors = []
    zero = {name: 0.0 for name in steps()}
    for count in (1, 12, len(zero)):
        if not all(split(zero, count)[0]):
            errors.append('steps that cost nothing leave a shard of %d empty' % count)
    for bad in ('nan', 'inf', '-1'):
        try:
            parse_costs('test:x\t%s\n' % bad)
            errors.append('a cost of %s is accepted' % bad)
        except AssertionError:
            pass
    return errors


def check():
    """Every package has one CI step, ci_steps are well formed, and for every shard count the
    shards' union is every CI step, each exactly once."""
    errors, everything = ['selftest: ' + e for e in selftest()], steps()
    for name in ci_steps():
        if not re.match(r'(test|check):[a-z][a-z0-9-]*$', name):
            errors.append('ci_steps: %r is not test:<name> or check:<name>' % name)
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


def main(args):
    if args[:1] == ['steps'] and len(args) == 1:
        print('\n'.join(steps()))
        return 0
    if args[:1] == ['shard'] and len(args) == 3:
        return shard(int(args[1]), int(args[2]))
    if args[:1] == ['costs'] and len(args) >= 2:
        return costs(args[1:])
    if args == ['check']:
        return check()
    sys.exit(__doc__)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
