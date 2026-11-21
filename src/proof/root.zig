//! `proof` group. Mirrors base/base `crates/proof`. Sub-modules are re-exported
//! here by package name as they land, e.g. `pub const rlp = @import("proof/rlp");`.

const std = @import("std");

pub const preimage = @import("proof/preimage");
pub const mpt = @import("proof/mpt");
pub const executor = @import("proof/executor");
pub const program = @import("proof/program");
pub const driver = @import("proof/driver");
pub const client = @import("proof/client");
pub const host = @import("proof/host");
pub const contracts = @import("proof/contracts");
pub const rpc = @import("proof/rpc");
pub const proposer = @import("proof/proposer");
pub const challenger = @import("proof/challenger");
pub const submission = @import("proof/submission");
pub const prover_service = @import("proof/prover_service");
pub const telemetry = @import("proof/telemetry");
pub const tee = @import("proof/tee");
pub const primitives = @import("proof/primitives");

test {
    std.testing.refAllDecls(@This());
    // The fault-proof client's end-to-end test drives the host, the proposer, the prover
    // service and the TEE, so it sits here, above every package it imports.
    _ = @import("e2e_test.zig");
    // Host and prover service tests that run the client, and with it the EVM.
    _ = @import("offline_test.zig");
    _ = @import("rpc_source_test.zig");
    _ = @import("native_backend_test.zig");
}
