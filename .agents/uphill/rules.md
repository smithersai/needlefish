# Rules

Uphill writes these rules from each metric's `description`. Write code that meets them. `uphill` scores each metric against its target, and `uphill --compare HEAD --compact` lists what a change made worse.

## Footprint

Keep every Base L2 Zig package focused, bounded in size, and strictly zero-dependency.

- Keep each Zig package cohesive and bounded in production line count; decompose overgrown packages along protocol boundaries. At most 1200.
- Keep each Zig package compact and avoid duplicating protocol or state-machine code across layers. At most 48 kB.
- Keep build.zig.zon completely free of external dependencies (Zig standard library only).

## Style & TigerStyle

Enforce needlefish TigerStyle limits from AGENTS.md: zig fmt, <=100 cols, <=70-line fns, and >=2.0 asserts/fn.

- Format all Zig source files and manifests with `zig fmt`.
- Keep every Zig function at or under 70 lines (TigerStyle rule); split state transitions into small helpers.
- Wrap Zig lines at or under 100 columns (TigerStyle rule in etc/scripts/tidy.sh).
- Assert preconditions, postconditions, and invariants aiming for >= 2 asserts per function (200 per 100 fns, TigerStyle). At least 200.

## Correctness

Enforce acyclic package imports, CI shard completeness, zero external deps, no flashblocks, and proved unreachable paths.

- Keep cross-package imports acyclic and regenerate `packages.zon` with `python3 -I etc/scripts/packages.py convert`.
- Keep `etc/scripts/ci-shards.py` covering every CI step across all shard counts.
- Never add external package dependencies to `build.zig.zon`.
- Return explicit Zig errors, or document every `catch unreachable` with an adjacent proof comment.
- Resolve TODO/FIXME markers instead of leaving them in production Zig packages.
- Do not add flashblocks code; needlefish implements native 200 ms L2 blocks only.

## Testing

Measure unit test depth and fuzz/DST harness presence across every Base L2 package.

- Maintain thorough inline unit tests covering happy paths, boundaries, and error cases in every src/ package. At least 25.
- Pair parsers, decoders, and state machines in every src/ package with `std.testing.fuzz` or seeded `Prng` fuzz tests.

## Documentation

Track public Zig API doc-comment coverage and ensure all repository Markdown links resolve.

- Document public Zig functions and constants with `///` doc comments (target >= 80%). At least 80.
- Keep all relative links in README.md, AGENTS.md, and docs/ valid.
