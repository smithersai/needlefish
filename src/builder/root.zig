//! `builder` group: the Base block builder (full blocks at the native 200 ms cadence, no
//! flashblocks). Mirrors base/base `crates/builder/{core,metering}`. Sub-modules are
//! re-exported here.

const std = @import("std");

pub const core = @import("builder/core");
pub const metering = @import("builder/metering");

test {
    std.testing.refAllDecls(@This());
}
