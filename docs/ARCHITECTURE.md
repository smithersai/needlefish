# Architecture

needlefish mirrors base/base's crate layout as a tree of Zig modules, one per package.

| base/base | needlefish |
| --- | --- |
| `bin/<name>` | `bin/<name>/main.zig` |
| `crates/common/<x>` | `src/common/<x>/root.zig` |
| `crates/utilities/<x>` | `src/utilities/<x>/root.zig` |
| `crates/execution/<x>` | `src/execution/<x>/root.zig` |
| `crates/consensus/<x>` | `src/consensus/<x>/root.zig` |
| `crates/batcher/<x>` | `src/batcher/<x>/root.zig` |
| `crates/proof/<x>` | `src/proof/<x>/root.zig` |
| `crates/infra/<x>` | `src/infra/<x>/root.zig` (phase 2) |
| `etc/docker` devnet | `etc/devnet` (native processes) |

## Principles

- **One module per package.** Each folder two levels under `src/` (`src/common/rlp`) is a
  Zig module with its own test binary; `packages.zon` lists the packages each one imports
  by name (`@import("common/rlp")`). Each file of a binary (`bin/base/up.zig`) is a
  package too, with its own test binary, and imports what it uses by name; `src/base.zig`
  re-exports every group for the tools that import it as `base`. `etc/scripts/packages.py`
  defines packages, rewrites imports, and checks the import graph.
- **IO is injected.** `src/utilities/io` provides io_uring (Linux), kqueue (macOS), and a
  simulated backend. Components take the IO type as a comptime parameter, so the same
  code runs in production and in the deterministic simulator.
- **State machines.** Networking, derivation, sequencing, batching, and txpool are event
  driven: they react to completions and ticks and never block.
- **Static memory.** Components size their pools from config at init.

## Data flow (devnet)

```
         deposits / batches (calldata, blobs)
 L1 (node --dev-l1) <──────────── batcher ◄── L2 sequencer EL (node)
        │                                          ▲ engine API
        │ L1 blocks, receipts, blobs               │
        ├────────────────► consensus (sequencer) ──┘
        │
        └────────────────► consensus (verifier) ──► L2 verifier EL (node)
```
