//! Offline mode: replay a recorded preimage archive through the fault-proof client
//! (base/base `backend/offline.rs`: stored keys only, hints ignored).

const std = @import("std");
const assert = std.debug.assert;

const preimage = @import("proof/preimage");
const client = @import("proof/client");
const program = @import("proof/program");
const archive = @import("archive.zig");
const store_module = @import("store.zig");

pub const Verdict = struct {
    outcome: client.Outcome,
    claimed_output_root: [32]u8,
    holds: bool,
};

pub const Error = archive.DecodeError || client.client.Error || error{ReplayFailed};

/// Runs the client over the preimages of `bytes` (an archive).
pub fn replay(
    allocator: std.mem.Allocator,
    bytes: []const u8,
    options: client.client.Options,
) Error!Verdict {
    var store: store_module.Store = .init(allocator, .{});
    defer store.deinit();
    try archive.decode(bytes, &store);
    var harness: preimage.InProcess = undefined;
    harness.init(allocator, store.fetcher(), preimage.in_process.ignore_hints, .{
        .channel_bytes = 1 << 20,
    }) catch return error.ReplayFailed;
    defer harness.deinit(allocator);
    const io: client.Client.Io = .{
        .oracle = harness.oracle_reader(),
        .hints = harness.hint_writer(),
    };
    const outcome = try client.run(allocator, io, options);
    var scratch: [program.boot.rollup_config_bytes_max]u8 = undefined;
    const boot = try program.BootInfo.load(harness.oracle_reader(), &scratch);
    const holds = client.claim_holds(&boot, &outcome);
    assert(holds == std.mem.eql(u8, &outcome.output_root, &boot.claimed_l2_output_root));
    const claimed = boot.claimed_l2_output_root;
    return .{ .outcome = outcome, .claimed_output_root = claimed, .holds = holds };
}
