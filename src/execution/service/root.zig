//! The execution node service: `Service(IO)` owns the node (execution/node), its eth and
//! Engine API JSON-RPC servers (execution/rpc) and the payload builder (builder/core), and
//! `base node` runs it. It sits above all three, so they do not import it.

const std = @import("std");

pub const service = @import("service.zig");
pub const Service = service.Service;
pub const bench_engine = @import("bench_engine.zig");

test {
    std.testing.refAllDecls(@This());
    _ = @import("service_test.zig");
    _ = @import("witness_memory_test.zig");
}
