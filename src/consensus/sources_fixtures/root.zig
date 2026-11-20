//! Test fixtures for `consensus/sources`: signed transactions, and L1 and L2 block and
//! receipt JSON written by the execution RPC's formatter (`execution/rpc` `format`), plus
//! the source tests that read them. They sit above both packages, so consensus/sources
//! does not import execution/rpc, which reaches it through execution/node.

const std = @import("std");

pub const fixtures = @import("fixtures.zig");

test {
    std.testing.refAllDecls(@This());
    _ = @import("header_json_test.zig");
    _ = @import("l1_json_test.zig");
    _ = @import("l1_provider_test.zig");
    _ = @import("engine_test.zig");
    _ = @import("l2_block_full_test.zig");
    _ = @import("l2_provider_test.zig");
}
