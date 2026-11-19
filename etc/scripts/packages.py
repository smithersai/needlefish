#!/usr/bin/env python3
"""needlefish packages: what a package is, the import graph, and the package table.

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

`convert` makes every package a Zig module (build.zig): inside a package, imports stay
relative paths; across packages they name the package, `@import("common/rlp")`. It also
writes packages.zon, which lists every package, its root file, and the names it imports.

usage: python3 -I etc/scripts/packages.py check      (run by etc/scripts/tidy.sh)
       python3 -I etc/scripts/packages.py allowlist  (bootstrap only: print a new list)
       python3 -I etc/scripts/packages.py convert    (named cross-package imports; packages.zon)
"""
import collections
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ALLOW = 'etc/import-cycles.allow'
TABLE = 'packages.zon'
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


Import = collections.namedtuple('Import', 'kind spec line start end')


def imports(text):
    """Every @import/@embedFile of a literal path, comment lines skipped; offsets into text."""
    text = COMMENT.sub(lambda m: ' ' * len(m.group(0)), text)
    return [Import(m.group(1), m.group(2), text.count('\n', 0, m.start()) + 1, m.start(), m.end())
            for m in IMPORT.finditer(text)]


def read_sources():
    """{path: text} for every .zig file under src/, the tool files, and the package set."""
    texts = {}
    for f in source_files():
        with open(os.path.join(ROOT, f), encoding='utf-8') as handle:
            texts[f] = handle.read()
    tools = {f for f in texts if any(i.spec == 'base' for i in imports(texts[f]))}
    packages = {package_of(f) for f in texts if f not in tools}
    return texts, tools, packages


def scan():
    """Package graph: {from: {to: [evidence]}}, the package set, and the tool files."""
    texts, tools, packages = read_sources()
    files = sorted(texts)
    graph = collections.defaultdict(lambda: collections.defaultdict(list))
    for f in files:
        if f in tools:
            continue
        here = package_of(f)
        graph[here]
        for i in imports(texts[f]):
            there = target_package(f, i.kind, i.spec, packages)
            if there is not None and there != here:
                graph[here][there].append('%s:%d %s' % (f, i.line, i.spec))
    return graph, packages, tools


def target_package(path, kind, spec, packages):
    """Package an import resolves to, or None for std/builtin/options."""
    if kind == 'import' and not spec.endswith('.zig'):
        if spec in BUILTIN_NAMES:
            return None
        assert spec in packages, '%s: unknown module %r' % (path, spec)
        return spec
    return package_of(resolve(path, spec))


def resolve(path, spec):
    """Repo-relative file a relative @import/@embedFile path names."""
    target = os.path.normpath(os.path.join(os.path.dirname(path), spec)).replace(os.sep, '/')
    assert target.startswith('src/'), '%s: %s leaves src/' % (path, spec)
    assert os.path.exists(os.path.join(ROOT, target)), '%s: %s is missing' % (path, spec)
    return target


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


# `convert`: rewrite every import that crosses a package boundary to the package's name.
#   @import("../../common/rlp/root.zig")      -> @import("common/rlp")
#   @import("../primitives/hex.zig")          -> @import("common/primitives").hex
#   @embedFile("../withdrawer/testdata/x.json") -> @import("infra/withdrawer").x_json
# A file reached through a package's pub declarations keeps that path; any other file (and
# every embedded data file) gets a `pub const` re-export added to the package root. Then
# write packages.zon, the package table build.zig reads. A second run changes nothing.

PUB_IMPORT = re.compile(r'^pub const (\w+) = @import\("([^"]+\.zig)"\);$', re.M)
EXPORTED = re.compile(r'^pub const (\w+) =\s*@(import|embedFile)\("([^"]+)"\);$', re.M)
TOP_NAME = re.compile(r'^(?:pub )?(?:extern )?(?:inline )?(?:const|var|fn) (\w+)', re.M)
ANCHOR = re.compile(r'^(?:pub )?const \w+ = @(?:import|embedFile)\("[^"]*"\)[\w.]*;$', re.M)
IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


class Exports:
    """The pub declarations other packages reach a package's files through, and the
    re-exports `convert` adds to package roots."""

    def __init__(self, texts):
        self.texts = texts
        self.added = collections.defaultdict(dict)  # root -> {relative path: name}
        self.embedded = collections.defaultdict(dict)  # root -> {relative path: name}

    def chain(self, package, target):
        """Field path from the package root to `target` over `pub const x = @import(...)`
        declarations of that package (shortest, declaration order), or None."""
        root = package_root(package)
        paths, frontier = {root: []}, [root]
        while frontier:
            following = []
            for f in frontier:
                found = PUB_IMPORT.findall(self.texts[f])
                if f == root:
                    found += [(n, r) for r, n in self.added.get(root, {}).items()]
                for name, relative in found:
                    t = resolve(f, relative)
                    if t not in paths and package_of(t) == package:
                        paths[t] = paths[f] + [name]
                        following.append(t)
            frontier = following
        return paths.get(target)

    def add(self, package, target, embed):
        """Name of the package root's `pub const` for `target`; a new one when it has none."""
        root = package_root(package)
        relative = os.path.relpath(target, os.path.dirname(root)).replace(os.sep, '/')
        kind = 'embedFile' if embed else 'import'
        for name, found_kind, found in EXPORTED.findall(self.texts[root]):
            if (found_kind, found) == (kind, relative):
                return name
        table = self.embedded[root] if embed else self.added[root]
        if relative in table:
            return table[relative]
        taken = set(TOP_NAME.findall(self.texts[root]))
        taken |= set(self.added[root].values()) | set(self.embedded[root].values())
        stem = relative if embed else relative[:-len('.zig')]
        for name in (os.path.basename(stem), stem):
            name = re.sub(r'[^A-Za-z0-9_]', '_', name)
            if IDENTIFIER.match(name) and name not in taken:
                table[relative] = name
                return name
        raise AssertionError('%s: no free name for %s' % (root, relative))

    def apply(self, root, text):
        """Root text with its new re-exports inserted after the last top-level import."""
        lines = ['pub const %s = @import("%s");' % (n, r) for r, n in self.added[root].items()]
        lines += ['pub const %s = @embedFile("%s");' % (n, r)
                  for r, n in self.embedded[root].items()]
        if not lines:
            return text
        lines = [l if len(l) <= 100 else l.replace(' = ', ' =\n    ', 1) for l in sorted(lines)]
        anchors = list(ANCHOR.finditer(text))
        assert anchors, '%s: no top-level import to add re-exports after' % root
        at = anchors[-1].end() + 1
        return text[:at] + '\n'.join(lines) + '\n' + text[at:]


def named(exports, path, item):
    """The named-import expression for one cross-package import, or None."""
    target = resolve(path, item.spec)
    package = package_of(target)
    if package == package_of(path):
        return None
    if item.kind == 'embedFile':
        return '@import("%s").%s' % (package, exports.add(package, target, embed=True))
    if target == package_root(package):
        return '@import("%s")' % package
    fields = exports.chain(package, target)
    if fields is None:
        fields = [exports.add(package, target, embed=False)]
    return '@import("%s").%s' % (package, '.'.join(fields))


def rewrite(texts, tools):
    """{path: new text} for every file with a cross-package import, roots included."""
    exports, out = Exports(texts), {}
    for f in sorted(texts):
        if f in tools:
            continue
        text = texts[f]
        for item in reversed(imports(text)):
            if item.kind == 'import' and not item.spec.endswith('.zig'):
                continue
            replacement = named(exports, f, item)
            if replacement is not None:
                text = text[:item.start] + replacement + text[item.end:]
        if text != texts[f]:
            out[f] = text
    for root in set(exports.added) | set(exports.embedded):
        text = exports.apply(root, out.get(root, texts[root]))
        if text != texts[root]:
            out[root] = text
    return out


def table(texts, tools, packages):
    """packages.zon text: every package, its root when not src/<name>/root.zig, its deps."""
    deps = {p: set() for p in packages}
    for f, text in texts.items():
        if f in tools:
            continue
        for item in imports(text):
            if item.kind == 'import' and not item.spec.endswith('.zig'):
                if item.spec == 'build_options' or item.spec in packages:
                    deps[package_of(f)].add(item.spec)
    lines = ['// Generated by `etc/scripts/packages.py convert`: one Zig module per package.',
             '// `deps` are the named imports of its files; build.zig reads this table.',
             '.{']
    for p in sorted(packages):
        root = package_root(p)
        head = '.name = "%s"' % p
        if root != 'src/%s/root.zig' % p:
            head += ', .root = "%s"' % root
        names = ', '.join('"%s"' % d for d in sorted(deps[p]))
        names = ' %s ' % names if len(deps[p]) > 1 else names  # as zig fmt prints it
        line = '    .{ %s, .deps = .{%s} },' % (head, names)
        if len(line) <= 100:
            lines.append(line)
            continue
        lines.append('    .{')
        lines.append('        %s,' % head.replace(', .root', ',\n        .root'))
        lines.append('        .deps = .{')
        lines += ['            "%s",' % d for d in sorted(deps[p])]
        lines += ['        },', '    },']
    return '\n'.join(lines + ['}', ''])


def convert():
    texts, tools, packages = read_sources()
    changed = rewrite(texts, tools)
    for f, text in sorted(changed.items()):
        with open(os.path.join(ROOT, f), 'w', encoding='utf-8') as handle:
            handle.write(text)
    texts.update(changed)
    with open(os.path.join(ROOT, TABLE), 'w', encoding='utf-8') as handle:
        handle.write(table(texts, tools, packages))
    print('packages: %d files rewritten, %d packages in %s' % (len(changed), len(packages), TABLE))
    return 0


if __name__ == '__main__':
    commands = {'check': check, 'allowlist': allowlist, 'convert': convert}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        sys.exit(__doc__)
    sys.exit(commands[sys.argv[1]]())
