//! The shared sender's fee bumping against a canned L1 over SimIo: a transaction without
//! a receipt is re-signed at its nonce with raised fees, and the bumped hash's receipt
//! settles it.

const std = @import("std");
const testing = std.testing;

const io = @import("utilities/io");
const harness_module = @import("consensus/sources").sim_harness;
const endpoint_module = @import("proof/rpc").endpoint;
const consensus = @import("common/consensus");
const rlp = @import("common/rlp");
const txdata = @import("batcher/txdata");
const accounts = @import("batcher/devnet").accounts;
const sender_module = @import("sender.zig");

const FakeRpc = harness_module.FakeRpc;
const Harness = harness_module.Harness(*FakeRpc, FakeRpc.handle);
const Endpoint = endpoint_module.Endpoint(io.SimIo);
const Sender = sender_module.Sender(Endpoint);

const target: sender_module.Address = @splat(0x7c);
const calldata = "\x12\x34\x56\x78bump";
const gas_estimate: u64 = 0x40000;
const base_fee: u64 = 9;
const nonce: u64 = 11;

fn step(context: *anyopaque) anyerror!void {
    const harness: *Harness = @ptrCast(@alignCast(context));
    harness.world.run_for_ns(std.time.ns_per_ms);
}

/// A canned L1 over SimIo for the submitter tests: the fake node's harness, a JSON-RPC
/// client to it, and an endpoint that pumps the harness while it waits.
pub const FakeL1 = struct {
    harness: Harness,
    client: Endpoint.Client,
    l1: Endpoint,

    /// `fake` holds its answers and outlives the L1; `self` must not move after this.
    pub fn init(self: *FakeL1, fake: *FakeRpc, seed: u64) !void {
        const allocator = testing.allocator;
        try self.harness.init(fake, seed);
        errdefer self.harness.deinit();
        const options = harness_module.client_options;
        const address = self.harness.address;
        self.client = try .init(allocator, &self.harness.client_io, address, options, 4096);
        errdefer self.client.deinit();
        const pump: endpoint_module.Pump = .{ .context = &self.harness, .step = step };
        self.l1 = .init(allocator, &self.client, pump, 100_000);
    }

    pub fn deinit(self: *FakeL1) void {
        self.l1.deinit();
        self.client.deinit();
        self.harness.deinit();
    }
};

/// A JSON-RPC `execution reverted` error carrying `data`.
pub fn revert_error(arena: std.mem.Allocator, data: []const u8) ![]const u8 {
    const format = "{{\"code\":3,\"message\":\"execution reverted\",\"data\":\"0x{x}\"}}";
    return std.fmt.allocPrint(arena, format, .{data});
}

/// Answers `debug_getRawHeader` with a header at `number` carrying `fee`.
pub fn add_raw_header(fake: *FakeRpc, arena: std.mem.Allocator, number: u64, fee: u64) !void {
    var buffer: [600]u8 = undefined;
    const header = rlp.encode_to(&buffer, consensus.Header{
        .number = number,
        .base_fee_per_gas = fee,
    });
    fake.add(.{
        .method = "debug_getRawHeader",
        .result = try std.fmt.allocPrint(arena, "\"0x{x}\"", .{header}),
    });
}

/// The raw transaction and hash the sender signs at `fees` (RFC 6979 is deterministic).
fn signed(
    arena: std.mem.Allocator,
    account: accounts.Account,
    fees: txdata.Fees,
) !struct { raw_hex: []const u8, hash: [32]u8 } {
    const envelope = try txdata.sign_eip1559(.{
        .chain_id = 900,
        .nonce = nonce,
        .fees = fees,
        .gas_limit = gas_estimate + gas_estimate / 100 * 20,
        .to = .{ .bytes = target },
        .value = 5,
        .data = calldata,
    }, account.private_key);
    const raw = try arena.alloc(u8, envelope.encoded_length());
    const encoded = txdata.encode_raw(envelope, raw);
    return .{
        .raw_hex = try std.fmt.allocPrint(arena, "{x}", .{encoded}),
        .hash = envelope.hash().bytes,
    };
}

test "submission sender: a stuck transaction is resubmitted at its nonce with bumped fees" {
    const allocator = testing.allocator;
    var arena_state: std.heap.ArenaAllocator = .init(allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();
    const account = accounts.derive_dev(5);
    const first = try signed(arena, account, txdata.suggest_fees(base_fee, 2));
    // 2 * 1.15 + 1 and 20 * 1.15 + 1 (integer), above today's suggestion.
    const bumped_fees: txdata.Fees = .{ .max_fee_per_gas = 24, .max_priority_fee_per_gas = 3 };
    const second = try signed(arena, account, bumped_fees);
    const fake = try allocator.create(FakeRpc);
    defer allocator.destroy(fake);
    fake.* = .{};
    fake.add(.{ .method = "eth_estimateGas", .result = "\"0x40000\"" });
    fake.add(.{ .method = "eth_getTransactionCount", .result = "\"0xb\"" });
    fake.add(.{ .method = "eth_maxPriorityFeePerGas", .result = "\"0x2\"" });
    try add_raw_header(fake, arena, 12, base_fee);
    for ([_]@TypeOf(first){ first, second }) |tx| fake.add(.{
        .method = "eth_sendRawTransaction",
        .params_contain = tx.raw_hex,
        .result = try std.fmt.allocPrint(arena, "\"0x{x}\"", .{&tx.hash}),
    });
    const receipt_second = fake.count;
    fake.add(.{
        .method = "eth_getTransactionReceipt",
        .params_contain = try std.fmt.allocPrint(arena, "{x}", .{&second.hash}),
        .result = "null",
    });
    fake.add(.{ .method = "eth_getTransactionReceipt", .result = "null" });
    var chain: FakeL1 = undefined;
    try chain.init(fake, 0x63);
    defer chain.deinit();
    var sender: Sender = try .init(allocator, &chain.l1, .{
        .chain_id = 900,
        .private_key = account.private_key,
        .from = account.address.bytes,
        .calldata_bytes_max = 64,
        .receipt_polls_max = 10,
        .resubmit_polls = 2,
        .bumps_max = 1,
    });
    defer sender.deinit(allocator);
    const sent = sender.send(.{ .to = target, .value = 5, .calldata = calldata });
    const id = sent.pending;
    // Two polls without a receipt, then the bump goes out at the same nonce.
    try testing.expect(sender.status(id) == .pending);
    try testing.expectEqual(@as(u64, 0), sender.bumps);
    try testing.expect(sender.status(id) == .pending);
    try testing.expectEqual(@as(u64, 1), sender.bumps);
    // No further bumps past `bumps_max`.
    for (0..3) |_| try testing.expect(sender.status(id) == .pending);
    try testing.expectEqual(@as(u64, 1), sender.bumps);
    fake.entries[receipt_second].result = "{\"status\":\"0x1\",\"blockNumber\":\"0xd\"}";
    try testing.expect(sender.status(id) == .mined);
    try testing.expect(sender.has_free_slot());
}
