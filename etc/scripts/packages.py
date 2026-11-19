#!/usr/bin/env python3
"""needlefish packages: the package-to-package import graph and its cycle check.

A package is a folder of src/ two levels deep, plus the files under it:
  src/<group>/<name>/**          package "<group>/<name>"
  src/<group>/*.zig              package "<group>" (the group root and its loose files)
  src/base.zig                   package "base" (the umbrella module binaries import)
  LEAVES below                   single-file packages: loose group files that subfolders
                                 import; inside "<group>" they would tie every subfolder
                                 to the group root, which imports every subfolder.
Files that `@import("base")` are tool entry points (`*_main.zig`), not package members.

Edge A -> B: a file of package A imports (`@import`, `@embedFile`) a file of package B.

The rule (`check`): the import graph minus the edges listed in etc/import-cycles.allow
must be acyclic. That graph is the "downward" order; an allowlisted edge is an "upward"
import that closes a cycle and is due to be removed. A line may be deleted, never added:
`check` also fails on a line whose edge no longer exists or no longer closes a cycle, so
the list only shrinks and a broken cycle cannot quietly come back.

usage: python3 -I etc/scripts/packages.py check      (run by etc/scripts/tidy.sh)
       python3 -I etc/scripts/packages.py allowlist  (bootstrap only: print a new list)
"""
import collections
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ALLOW = 'etc/import-cycles.allow'
LEAVES = {
    'src/testing/prng.zig': 'testing/prng',
    'src/testing/fuzz.zig': 'testing/fuzz',
    'src/testing/bench.zig': 'testing/bench',
    'src/proof/test_oracle.zig': 'proof/test_oracle',
}
BUILTIN_NAMES = {'std', 'builtin', 'root', 'build_options'}
IMPORT = re.compile(r'@(import|embedFile)\(\s*"([^"\\]*)"\s*,?\s*\)')
COMMENT = re.compile(r'^[ \t]*//.*$', re.M)


def package_of(path):
    """Package of a repo-relative `src/...` path (source or data file)."""
    assert path.startswith('src/'), path
    if path in LEAVES:
        return LEAVES[path]
    parts = path.split('/')
    if len(parts) == 2:
        return 'base'
    if len(parts) == 3:
        return parts[1]
    return parts[1] + '/' + parts[2]


def package_root(name):
    """Root source file of a package, repo-relative."""
    for path, leaf in LEAVES.items():
        if leaf == name:
            return path
    return 'src/base.zig' if name == 'base' else 'src/' + name + '/root.zig'


def source_files():
    out = []
    for directory, _, files in os.walk(os.path.join(ROOT, 'src')):
        for f in files:
            if f.endswith('.zig'):
                out.append(os.path.relpath(os.path.join(directory, f), ROOT).replace(os.sep, '/'))
    return sorted(out)


def imports(text):
    """(kind, spec, line) for every @import/@embedFile with a literal path, comments skipped."""
    text = COMMENT.sub('', text)
    return [(m.group(1), m.group(2), text.count('\n', 0, m.start()) + 1)
            for m in IMPORT.finditer(text)]


def scan():
    """Package graph: {from: {to: [evidence]}}, the package set, and the tool files."""
    files = source_files()
    texts = {f: open(os.path.join(ROOT, f), encoding='utf-8').read() for f in files}
    tools = {f for f in files if any(s == 'base' for _, s, _ in imports(texts[f]))}
    packages = {package_of(f) for f in files if f not in tools}
    graph = collections.defaultdict(lambda: collections.defaultdict(list))
    for f in files:
        if f in tools:
            continue
        here = package_of(f)
        graph[here]
        for kind, spec, line in imports(texts[f]):
            there = target_package(f, kind, spec, packages)
            if there is not None and there != here:
                graph[here][there].append('%s:%d %s' % (f, line, spec))
    return graph, packages, tools


def target_package(path, kind, spec, packages):
    """Package an import resolves to, or None for std/builtin/options."""
    if kind == 'import' and not spec.endswith('.zig'):
        if spec in BUILTIN_NAMES:
            return None
        assert spec in packages, '%s: unknown module %r' % (path, spec)
        return spec
    target = os.path.normpath(os.path.join(os.path.dirname(path), spec)).replace(os.sep, '/')
    assert target.startswith('src/'), '%s: %s leaves src/' % (path, spec)
    assert os.path.exists(os.path.join(ROOT, target)), '%s: %s is missing' % (path, spec)
    return package_of(target)


def read_allowlist():
    """{(from, to): line number}; malformed lines are reported and fail the check."""
    edges, errors = {}, []
    with open(os.path.join(ROOT, ALLOW), encoding='utf-8') as f:
        for number, raw in enumerate(f, 1):
            line = raw.split('#', 1)[0].strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split('->')]
            if len(parts) != 2 or not all(parts):
                errors.append('%s:%d: expected "from -> to": %s' % (ALLOW, number, raw.strip()))
            elif tuple(parts) in edges:
                errors.append('%s:%d: duplicate edge %s' % (ALLOW, number, line))
            else:
                edges[tuple(parts)] = number
    return edges, errors


def find_cycle(nodes, successors):
    """One cycle as a node list (first == last), or None. Iterative DFS, sorted order."""
    state = {}  # absent: unvisited, 1: on the stack, 2: done
    for start in sorted(nodes):
        if start in state:
            continue
        stack = [(start, iter(sorted(successors(start))))]
        state[start] = 1
        while stack:
            node, children = stack[-1]
            child = next(children, None)
            if child is None:
                state[node] = 2
                stack.pop()
            elif state.get(child) == 1:
                path = [n for n, _ in stack]
                return path[path.index(child):] + [child]
            elif child not in state:
                state[child] = 1
                stack.append((child, iter(sorted(successors(child)))))
    return None


def reaches(graph, removed, source, target):
    """True when `target` is reachable from `source` over edges not in `removed`."""
    seen, frontier = {source}, [source]
    while frontier:
        node = frontier.pop()
        for child in graph.get(node, ()):
            if (node, child) not in removed and child not in seen:
                if child == target:
                    return True
                seen.add(child)
                frontier.append(child)
    return False


def unneeded(graph, edges):
    """Allowlisted edges, tested in order, that close no cycle. A needed edge stays removed;
    an unneeded one goes back into the graph before the next test, so deleting every
    unneeded line still leaves the graph acyclic."""
    removed, out = set(edges), []
    for a, b in edges:
        if not reaches(graph, removed, b, a):
            removed.discard((a, b))
            out.append((a, b))
    return out


def check():
    graph, _, _ = scan()
    allowed, errors = read_allowlist()
    in_order = sorted(allowed, key=lambda edge: allowed[edge])
    present = [(a, b) for a, b in in_order if b in graph.get(a, {})]
    for a, b in in_order:
        if (a, b) not in present:
            errors.append('%s:%d: %s -> %s no longer exists; delete the line' %
                          (ALLOW, allowed[(a, b)], a, b))
    cycle = find_cycle(graph, lambda a: [b for b in graph[a] if (a, b) not in allowed])
    if cycle:
        errors.append('import cycle outside %s (break one edge; never add a line):' % ALLOW)
        for a, b in zip(cycle, cycle[1:]):
            errors.append('  %s -> %s   %s' % (a, b, graph[a][b][0]))
    else:
        for a, b in unneeded(graph, present):
            errors.append('%s:%d: %s -> %s closes no cycle now; delete the line' %
                          (ALLOW, allowed[(a, b)], a, b))
    for e in errors:
        print('packages: ' + e)
    return 1 if errors else 0


def feedback_edges(graph):
    """Edges whose removal leaves the graph acyclic: Eades-Lin-Smyth order weighted by import
    count, then adjacent swaps while they lower the weight of the backward edges."""
    weight = lambda a, b: len(graph[a].get(b, ()))
    left, head, tail = set(graph), [], []
    while left:
        moved = True
        while moved:
            moved = False
            for v in sorted(left):
                if not any(b in left for b in graph[v]):
                    tail.insert(0, v); left.discard(v); moved = True
            for v in sorted(left):
                if not any(v in graph[a] for a in left if a != v):
                    head.append(v); left.discard(v); moved = True
        if left:
            delta = lambda v: (sum(weight(v, b) for b in graph[v] if b in left) -
                               sum(weight(a, v) for a in left if v in graph[a]))
            v = max(sorted(left), key=delta)
            head.append(v); left.discard(v)
    order = head + tail
    improved = True
    while improved:
        improved = False
        for i in range(len(order) - 1):
            a, b = order[i], order[i + 1]
            if weight(b, a) > weight(a, b):
                order[i], order[i + 1] = b, a; improved = True
    position = {v: i for i, v in enumerate(order)}
    return sorted((a, b) for a in graph for b in graph[a] if position[a] > position[b])


def allowlist():
    graph, _, _ = scan()
    print('# Package imports that close a cycle (from -> to): etc/scripts/packages.py check.')
    print('# Delete a line when its cycle is gone. Never add one: break the cycle instead.')
    edges = feedback_edges(graph)
    drop = set(unneeded(graph, edges))
    for a, b in edges:
        if (a, b) not in drop:
            print('%s -> %s' % (a, b))
    return 0


if __name__ == '__main__':
    commands = {'check': check, 'allowlist': allowlist}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        sys.exit(__doc__)
    sys.exit(commands[sys.argv[1]]())
