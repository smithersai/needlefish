//! `batcher` group. Mirrors base/base `crates/batcher`: turns L2 blocks into channels,
//! frames, and L1 transactions (calldata or blobs).

const std = @import("std");

pub const frame = @import("batcher/frame");
pub const blobs = @import("batcher/blobs");
pub const comp = @import("batcher/comp");
pub const channel = @import("batcher/channel");
pub const devnet = @import("batcher/devnet");
pub const devnet_accounts = @import("batcher/devnet_accounts");
pub const txdata = @import("batcher/txdata");
pub const rpc = @import("batcher/rpc");
pub const source = @import("batcher/source");
pub const txmgr = @import("batcher/txmgr");
pub const service = @import("batcher/service");
pub const metrics = @import("batcher/metrics");
pub const throttle = @import("batcher/throttle");
pub const admin = @import("batcher/admin");
pub const sync = @import("batcher/sync");
pub const sequencers = @import("batcher/sequencers");
pub const parity = @import("batcher/parity");

test {
    std.testing.refAllDecls(@This());
}
