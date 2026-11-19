//! needlefish: Base, the Ethereum L2, rewritten in Zig.
//!
//! This is the root of the `base` module that binaries and tools import. Groups
//! mirror base/base's `crates/` directory. Each group root re-exports its
//! packages. Every package is its own module with its own test binary
//! (packages.zon, build.zig).

const std = @import("std");

pub const common = @import("common");
pub const utilities = @import("utilities");
pub const execution = @import("execution");
pub const consensus = @import("consensus");
pub const batcher = @import("batcher");
pub const proof = @import("proof");
pub const builder = @import("builder");
pub const infra = @import("infra");
pub const testing = @import("testing");

pub const build_options = @import("build_options");

test {
    std.testing.refAllDecls(@This());
}
