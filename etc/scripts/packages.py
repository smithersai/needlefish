#!/usr/bin/env python3
"""needlefish packages: what a package is, the import graph, and the package table.

A package is a folder of src/ two levels deep, plus the files under it:
  src/<group>/<name>/**          package "<group>/<name>"
  src/<group>/*.zig              package "<group>" (the group root and its loose files)
  src/base.zig                   package "base" (the umbrella module tools import)
  LEAVES below                   single-file packages: loose group files that subfolders
                                 import; inside "<group>" they would tie every subfolder
                                 to the group root, which imports every subfolder.
Tool entry points (`src/**/*_main.zig`), and the files only tools import, are not package
members; a tool imports packages by name or all of them as `base`.

A binary (bin/<program>/) is a package per file, so a test binary reruns only when a
package its file imports changes; a binary's file never imports `base`:
  bin/<program>/main.zig         package "bin/<program>" (the executable's root)
  bin/<program>/<file>.zig       package "bin/<program>/<file>"
  JOINED below                   files in an import cycle with their package's root

A package without a named test, whose unnamed `test {}` blocks only reference declarations
(`std.testing.refAllDecls(...)`, `_ = x;`), is a compile check: packages.zon marks it
`.compile_only = true`; `zig build check` compiles its test binary, `zig build test` skips it.

Edge A -> B: a file of package A imports (`@import`, `@embedFile`) a file of package B.

The rule (`check`): the import graph is acyclic, with no exception. Packages in a cycle
rerun each other's tests on every edit. For each cycle, `check` names its packages, a
shortest cycle among them, and the files whose imports form each edge of that cycle.
Break one edge: move the declaration both packages use down into the lower package, or
move the test that imports the upper package up, into it or a package above both.

`convert` makes every package a Zig module (build.zig): inside a package, imports stay
relative paths; across packages they name the package, `@import("common/rlp")`. It also
writes packages.zon, which lists every package, its root file, and the names it imports.

`check` also fails on a relative import that crosses a package boundary and on a
packages.zon that `convert` would change.

usage: python3 -I etc/scripts/packages.py check    (run by etc/scripts/tidy.sh; runs selftest)
       python3 -I etc/scripts/packages.py selftest (in-memory fixtures of the rules above)
       python3 -I etc/scripts/packages.py convert  (named cross-package imports; packages.zon)
"""
import collections
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TABLE = 'packages.zon'
CYCLE_FILES_SHOWN = 5  # importing files `check` lists per edge of a cycle
LEAVES = {
    'src/testing/prng.zig': 'testing/prng',
    'src/testing/fuzz.zig': 'testing/fuzz',
    'src/testing/bench.zig': 'testing/bench',
    'src/proof/test_oracle.zig': 'proof/test_oracle',
}
JOINED = {
    'bin/base/el_engine_test.zig': 'bin/base/el_engine',
    'bin/base/el_rpc_test.zig': 'bin/base/el_rpc',
    'bin/base/replay_bench.zig': 'bin/base/replay',
    'bin/base/replay_evm.zig': 'bin/base/replay',
    'bin/base/replay_fetch.zig': 'bin/base/replay',
    'bin/base/snapshot_import_out.zig': 'bin/base/snapshot_import',
}
BUILTIN_NAMES = {'std', 'builtin', 'root', 'build_options'}
IMPORT = re.compile(r'@(import|embedFile)\(\s*"([^"\\]*)"\s*,?\s*\)')
COMMENT = re.compile(r'^[ \t]*//.*$', re.M)
NAMED_TEST = re.compile(r'^[ \t]*test[ \t]+["A-Za-z_]', re.M)
UNNAMED_TEST = re.compile(r'^([ \t]*)test[ \t]*\{\n(.*?)^\1\}', re.M | re.S)
TEST_DECL = re.compile(r'^[ \t]*test\b', re.M)
PATH = r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*'
# The statements a compile check's test block may hold: each only makes the compiler analyze a
# declaration. Anything else (a call, a `try`, a trailing comment) keeps the package a run test.
REFERENCE = re.compile(r'^[ \t]*(?:_ = @import\("[^"\\]*"\)(?:\.%s)?|_ = %s|'
                       r'std\.testing\.refAllDecls\((?:@This\(\)|%s)\));[ \t]*$' %
                       (PATH, PATH, PATH))


def package_of(path):
    """Package of a repo-relative `src/...` or `bin/...` path (source or data file)."""
    if path in LEAVES:
        return LEAVES[path]
    if path in JOINED:
        return JOINED[path]
    parts = path.split('/')
    if parts[0] == 'bin':
        assert len(parts) == 3 and path.endswith('.zig'), path
        if parts[2] == 'main.zig':
            return 'bin/' + parts[1]
        return path[:-len('.zig')]
    assert parts[0] == 'src', path
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
    if name.startswith('bin/'):
        return name + ('/main.zig' if name.count('/') == 1 else '.zig')
    return 'src/base.zig' if name == 'base' else 'src/' + name + '/root.zig'


def source_files():
    out = []
    for top in ('src', 'bin'):
        for directory, _, files in os.walk(os.path.join(ROOT, top)):
            for f in files:
                if f.endswith('.zig'):
                    path = os.path.relpath(os.path.join(directory, f), ROOT)
                    out.append(path.replace(os.sep, '/'))
    return sorted(out)


Import = collections.namedtuple('Import', 'kind spec line start end')


def imports(text):
    """Every @import/@embedFile of a literal path, comment lines skipped; offsets into text."""
    text = COMMENT.sub(lambda m: ' ' * len(m.group(0)), text)
    return [Import(m.group(1), m.group(2), text.count('\n', 0, m.start()) + 1, m.start(), m.end())
            for m in IMPORT.finditer(text)]


def read_sources():
    """{path: text} for every .zig file under src/ and bin/, the tool files, and the package
    set."""
    texts = {}
    for f in source_files():
        with open(os.path.join(ROOT, f), encoding='utf-8') as handle:
            texts[f] = handle.read()
    tools = tool_files(texts)
    packages = {package_of(f) for f in texts if f not in tools}
    return texts, tools, packages


def tool_files(texts):
    """Tool entry points (`src/**/*_main.zig`) and the files only tools import (relatively),
    package roots excepted."""
    tools = {f for f in texts if f.startswith('src/') and f.endswith('_main.zig')}
    importers = collections.defaultdict(set)
    for f in texts:
        for i in imports(texts[f]):
            if i.kind == 'import' and i.spec.endswith('.zig'):
                importers[resolve(f, i.spec)].add(f)
    for _ in range(len(texts)):  # each pass adds a file or ends the loop
        more = {f for f in texts if f not in tools and importers[f] and importers[f] <= tools
                and f != package_root(package_of(f))}
        if not more:
            break
        tools |= more
    return tools


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
    top = path.split('/')[0]
    assert target.startswith(top + '/'), '%s: %s leaves %s/' % (path, spec, top)
    assert os.path.exists(os.path.join(ROOT, target)), '%s: %s is missing' % (path, spec)
    return target


def reachable(graph, source):
    """Every package reachable from `source` over one or more import edges."""
    seen, frontier = set(), [source]
    while frontier:
        for child in graph.get(frontier.pop(), ()):
            if child not in seen:
                seen.add(child)
                frontier.append(child)
    return seen


def cycles(graph):
    """Each set of packages that all reach each other (a strongly connected component of
    two or more packages), sorted; an empty list when the graph is acyclic."""
    reach = {node: reachable(graph, node) for node in graph}
    out, placed = [], set()
    for node in sorted(graph):
        if node in reach[node] and node not in placed:
            component = sorted(b for b in reach[node] if node in reach.get(b, ()))
            placed.update(component)
            out.append(component)
    return out


def shortest_cycle(graph, component):
    """A shortest cycle among the component's packages: a list whose last is its first."""
    inside, best = set(component), None
    for start in component:
        parent, order = {start: None}, [start]
        for node in order:  # breadth first: `order` grows while the loop reads it
            if start in graph[node]:
                path, at = [], node
                while at is not None:
                    path.append(at)
                    at = parent[at]
                if best is None or len(path) + 1 < len(best):
                    best = path[::-1] + [start]
                break
            for child in sorted(graph[node]):
                if child in inside and child not in parent:
                    parent[child] = node
                    order.append(child)
    assert best is not None and len(best) >= 3, component
    return best


def cycle_errors(graph, component):
    """Lines that report one cycle: its packages, a shortest cycle, the importing files."""
    cycle = shortest_cycle(graph, component)
    lines = ['import cycle among %d packages: %s' % (len(component), ', '.join(component)),
             '  shortest cycle: ' + ' -> '.join(cycle)]
    for a, b in zip(cycle, cycle[1:]):
        first = {}  # importing file -> its first import of b, as file:line
        for site in graph[a][b]:
            first.setdefault(site.split(':', 1)[0], site.split(' ', 1)[0])
        sites = list(first.values())
        lines.append('  %s -> %s, imported in %d file%s:' %
                     (a, b, len(sites), '' if len(sites) == 1 else 's'))
        lines += ['    ' + site for site in sites[:CYCLE_FILES_SHOWN]]
        if len(sites) > CYCLE_FILES_SHOWN:
            lines.append('    and %d more' % (len(sites) - CYCLE_FILES_SHOWN))
    return lines + [
        '  No import cycle is allowed: packages in a cycle rerun each other\'s tests.',
        '  Break one edge: move the declaration both sides use down into the lower package,',
        '  or move the test that imports the upper package up, into it or a package above both.',
    ]


def check():
    errors = ['selftest: ' + f for f in selftest()]
    graph, _, _ = scan()
    errors += table_errors() if os.path.exists(os.path.join(ROOT, TABLE)) else []
    for component in cycles(graph):
        errors += cycle_errors(graph, component)
    for e in errors:
        print('packages: ' + e)
    return 1 if errors else 0


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


def compile_checks(texts, tools, packages):
    """Packages whose tests only reference declarations: every `test` of their files is an
    unnamed block on lines of its own, and every statement in it is one REFERENCE names
    (`_ = @import("...");`, `_ = a.b;` with no call, `std.testing.refAllDecls(...);`). A
    test this cannot parse or a statement it does not recognise keeps the package a run test."""
    runs = set()
    for f, text in texts.items():
        if f in tools:
            continue
        text = COMMENT.sub('', text)
        blocks = [body for _, body in UNNAMED_TEST.findall(text)]
        lines = [line for body in blocks for line in body.split('\n') if line.strip()]
        parsed = len(blocks) == len(TEST_DECL.findall(text))
        if not parsed or NAMED_TEST.search(text) or not all(REFERENCE.match(l) for l in lines):
            runs.add(package_of(f))
    return packages - runs


def selftest_compile_only():
    """compile_checks on in-memory packages: (fixture, test text, compile-only expected)."""
    cases = [
        ('references only', 'test {\n    std.testing.refAllDecls(@This());\n'
         '    _ = @import("x.zig");\n    _ = @import("g/b").c;\n    _ = helper.sub;\n}\n',
         True),
        ('no test', 'pub const x = 1;\n', True),
        ('a call in `_ =`', 'test {\n    _ = runtime_check();\n}\n', False),
        ('a one-line unnamed test', 'test { try std.testing.expect(ok()); }\n', False),
        ('a named test', 'test "x" {\n    _ = y;\n}\n', False),
        ('a trailing comment', 'test {\n    _ = y; // and more\n}\n', False),
    ]
    failures = []
    for name, text, expected in cases:
        got = 'g/a' in compile_checks({'src/g/a/root.zig': text}, set(), {'g/a'})
        if got != expected:
            failures.append('compile-only %s: got %s, want %s' % (name, got, expected))
    return failures


def selftest_tool_imports():
    """import_errors in memory: neither a src/ package file nor a tool may import a binary."""
    failures = []
    for f, tools in (('src/g/a/root.zig', set()), ('src/g/a/x_main.zig', {'src/g/a/x_main.zig'})):
        errors = import_errors({f: 'const up = @import("bin/b/up");\n'}, tools)
        if not any("bin/b/up is a binary's package" in e for e in errors):
            failures.append('%s importing bin/b/up: no error, want one' % f)
    return failures


def selftest():
    """Failures of the in-memory fixtures (`packages.py selftest`; `check` runs them)."""
    failures = []
    for case in (selftest_compile_only, selftest_tool_imports):
        failures += case()
    return failures


def run_selftest():
    failures = selftest()
    for f in failures:
        print('packages: selftest: ' + f)
    return 1 if failures else 0


def table(texts, tools, packages):
    """packages.zon text: every package, its root when not src/<name>/root.zig, its deps, and
    whether its test binary is only a compile check."""
    deps = {p: set() for p in packages}
    for f, text in texts.items():
        if f in tools:
            continue
        for item in imports(text):
            if item.kind == 'import' and not item.spec.endswith('.zig'):
                if item.spec == 'build_options' or item.spec in packages:
                    deps[package_of(f)].add(item.spec)
    checks = compile_checks(texts, tools, packages)
    lines = ['// Generated by `etc/scripts/packages.py convert`: one Zig module per package.',
             '// `deps` are the named imports of its files; build.zig reads this table.',
             '.{']
    for p in sorted(packages):
        root = package_root(p)
        head = '.name = "%s"' % p
        if root != 'src/%s/root.zig' % p:
            head += ', .root = "%s"' % root
        tail = ', .compile_only = true' if p in checks else ''
        names = ', '.join('"%s"' % d for d in sorted(deps[p]))
        names = ' %s ' % names if len(deps[p]) > 1 else names  # as zig fmt prints it
        line = '    .{ %s, .deps = .{%s}%s },' % (head, names, tail)
        if len(line) <= 100:
            lines.append(line)
            continue
        lines.append('    .{')
        lines.append('        %s,' % head.replace(', .root', ',\n        .root'))
        lines.append('        .deps = .{')
        lines += ['            "%s",' % d for d in sorted(deps[p])]
        lines.append('        },')
        if tail:
            lines.append('        .compile_only = true,')
        lines.append('    },')
    return '\n'.join(lines + ['}', ''])


def import_errors(texts, tools):
    """Relative imports that cross a package boundary, a binary's file that imports `base`,
    and a src/ file, a tool's included, that imports a binary's package."""
    errors = []
    for f in sorted(texts):
        for item in imports(texts[f]):
            if item.kind == 'import' and not item.spec.endswith('.zig'):
                if f.startswith('bin/') and item.spec == 'base':
                    errors.append('%s:%d: base is every package; import the ones this binary '
                                  'file uses by name' % (f, item.line))
                if f.startswith('src/') and item.spec.startswith('bin/'):
                    errors.append('%s:%d: %s is a binary\'s package; a src/ file must not '
                                  'import it' % (f, item.line, item.spec))
                continue
            if f in tools:  # a tool's relative imports are its own files
                continue
            target = package_of(resolve(f, item.spec))
            if target != package_of(f):
                errors.append('%s:%d: %s is in package %s; import it by name (convert)' %
                              (f, item.line, item.spec, target))
    return errors


def table_errors():
    """import_errors, and a stale packages.zon."""
    texts, tools, packages = read_sources()
    errors = import_errors(texts, tools)
    with open(os.path.join(ROOT, TABLE), encoding='utf-8') as handle:
        if handle.read() != table(texts, tools, packages):
            errors.append('%s is stale; run etc/scripts/packages.py convert' % TABLE)
    return errors


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
    commands = {'check': check, 'convert': convert, 'selftest': run_selftest}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        sys.exit(__doc__)
    sys.exit(commands[sys.argv[1]]())
