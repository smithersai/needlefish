//! Online data sources for the consensus node: a non-blocking caching L1 chain provider
//! over JSON-RPC, a beacon blob provider, and an L2 engine API client.
//! Mirrors base/base `crates/consensus/providers` (`AlloyChainProvider`,
//! `OnlineBeaconClient`, `OnlineBlobProvider`) and `crates/consensus/engine/src/client.rs`.

const std = @import("std");

pub const json_object = @import("json_object.zig");
pub const header_json = @import("header_json.zig");
pub const transaction_json = @import("transaction_json.zig");
pub const receipt_json = @import("receipt_json.zig");
pub const l1_provider = @import("l1_provider.zig");
pub const L1Provider = l1_provider.L1Provider;
pub const beacon_json = @import("beacon_json.zig");
pub const blob_provider = @import("blob_provider.zig");
pub const BlobProvider = blob_provider.BlobProvider;
pub const engine_json = @import("engine_json.zig");
pub const l2_block = @import("l2_block.zig");
pub const l2_block_full = @import("l2_block_full.zig");
pub const engine_client = @import("engine_client.zig");
pub const EngineClient = engine_client.EngineClient;
pub const l2_provider = @import("l2_provider.zig");
pub const L2Provider = l2_provider.L2Provider;
pub const message_passer_roots = @import("message_passer_roots.zig");
pub const sepolia_l1_12090253_header_json = @embedFile("testdata/sepolia_l1_12090253_header.json");
pub const sim_harness = @import("sim_harness.zig");

// The JSON fixtures, which execution/rpc's formatter writes, and the tests that read them
// live in consensus/sources_fixtures, above both packages.
test {
    std.testing.refAllDecls(@This());
    _ = @import("blob_provider_test.zig");
}
