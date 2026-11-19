//! needlefish: Base, the Ethereum L2, rewritten in Zig.
//!
//! This is the root of the single `base` module. Groups mirror base/base's
//! `crates/` directory. Each group root re-exports its sub-modules. Every
//! declaration reachable from here is compiled and tested by `zig build test`.

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
