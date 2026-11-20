//! Devnet tooling: dev accounts, genesis and rollup config generation for
//! `base devnet init` (docs/DEVNET.md).

const std = @import("std");

pub const accounts = @import("batcher/devnet_accounts");
pub const portal = @import("portal.zig");
pub const genesis = @import("genesis.zig");
pub const init = @import("init.zig");
pub const transfer = @import("transfer.zig");
pub const inspect = @import("inspect.zig");
pub const base_time_proxy_hex = @embedFile("base_time_proxy.hex");

test {
    std.testing.refAllDecls(@This());
}
