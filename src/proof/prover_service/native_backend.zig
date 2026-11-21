//! A worker `Backend` that proves `compressed` range requests by running the fault-proof
//! program natively (`host/native.zig`) against an `RpcSource`, and returns the recorded
//! witness archive (`host/archive.zig`) as the proof bytes.
//!
//! This stands in for base/base's SP1 and TEE backends, whose proof formats (SP1 STARK and
//! SNARK receipts, Nitro enclave signatures) are out of scope: the archive is not succinct,
//! but anyone can re-check the claim by replaying it offline (`base proof replay`). The
//! boot values follow `base proof run`: the agreed output is block `start`, the claim is
//! block `start + count`, and the L1 head is the request's or the latest L1 block. A claim
//! that does not hold fails the job (the worker abandons it).
//!
//! Proving runs to completion inside `start`; `poll` then hands out the result.

const std = @import("std");
const assert = std.debug.assert;

const protocol = @import("protocol.zig");
const worker_module = @import("worker.zig");
const host = @import("proof/host");
const proof_client = @import("proof/client");
const crypto = @import("common/crypto");
const kzg = @import("common/crypto").kzg;

const BackendError = worker_module.BackendError;

pub const Config = struct {
    rollup_config_json: []const u8,
    l1_config_json: []const u8 = "",
    chain_id: u64,
    client_options: proof_client.client.Options,
    roots_brp: ?*const [kzg.field_elements_per_blob][32]u8 = null,
    /// Largest archive the backend returns (and the service accepts).
    proof_bytes_max: u32,
};

/// The backend over the node's JSON-RPC (`host.rpc_source`).
pub fn Native(comptime IO: type) type {
    return NativeOver(host.rpc_source.RpcSource(IO));
}

/// The backend over any source with `header_by_number(chain, ?number) ![]const u8` (the
/// raw header; null is the latest) and `as_source() host.handler.Source`.
pub fn NativeOver(comptime SourceType: type) type {
    return struct {
        const Self = @This();
        pub const Source = SourceType;

        allocator: std.mem.Allocator,
        source: *Source,
        config: Config,
        archive_buffer: []u8,
        outcome: ?(BackendError!protocol.ProofResult) = null,
        /// Proofs produced and claims that did not hold, for diagnostics.
        proved: u64 = 0,
        refuted: u64 = 0,

        pub fn init(allocator: std.mem.Allocator, source: *Source, config: Config) !Self {
            assert(config.proof_bytes_max >= host.archive.header_bytes);
            assert(config.rollup_config_json.len > 0);
            return .{
                .allocator = allocator,
                .source = source,
                .config = config,
                .archive_buffer = try allocator.alloc(u8, config.proof_bytes_max),
            };
        }

        pub fn deinit(native: *Self) void {
            native.allocator.free(native.archive_buffer);
            native.* = undefined;
        }

        pub fn backend(native: *Self) worker_module.Backend {
            return .{ .context = native, .vtable = &vtable };
        }

        const vtable: worker_module.Backend.VTable = .{
            .start = start,
            .poll = poll,
            .cancel = cancel,
        };

        fn of(context: *anyopaque) *Self {
            return @ptrCast(@alignCast(context));
        }

        fn start(context: *anyopaque, job: *const protocol.ProofJob) BackendError!void {
            const native = of(context);
            const range = switch (job.request.request) {
                .compressed => |zk| zk,
                .snark_plonk, .tee => return error.Unsupported,
            };
            native.outcome = native.prove(&range);
        }

        fn poll(context: *anyopaque) BackendError!?protocol.ProofResult {
            const native = of(context);
            const outcome = native.outcome orelse return null;
            native.outcome = null;
            return try outcome;
        }

        fn cancel(context: *anyopaque) void {
            of(context).outcome = null;
        }

        fn prove(
            native: *Self,
            range: *const protocol.ZkProofRequest,
        ) BackendError!protocol.ProofResult {
            const claimed_block = range.validate_range() catch return error.Failed;
            var store: host.Store = .init(native.allocator, .{});
            defer store.deinit();
            native.boot(&store, range, claimed_block) catch return error.Failed;
            var witness: host.Store = .init(native.allocator, .{});
            defer witness.deinit();
            const verdict = host.native.run(
                native.allocator,
                &store,
                native.source.as_source(),
                native.config.roots_brp,
                native.config.client_options,
                &witness,
            ) catch return error.Failed;
            if (!verdict.holds) {
                native.refuted += 1;
                return error.Failed;
            }
            const archive = try native.encode_archive(&witness);
            native.proved += 1;
            assert(archive.len >= host.archive.header_bytes);
            return .{ .compressed = .{ .zk_vm = range.zk_vm, .proof = archive } };
        }

        /// The boot values and the agreed output root preimage (`base proof run`'s `boot`).
        fn boot(
            native: *Self,
            store: *host.Store,
            range: *const protocol.ZkProofRequest,
            claimed_block: u64,
        ) !void {
            assert(claimed_block > range.start_block_number);
            const source = native.source;
            const agreed = try host.native.output_of(
                try source.header_by_number(.l2, range.start_block_number),
            );
            const agreed_root = try store.put_keccak(&agreed.encode());
            const claimed_root = try host.native.output_root_of(
                try source.header_by_number(.l2, claimed_block),
            );
            const l1_head = range.l1_head orelse
                crypto.keccak256(try source.header_by_number(.l1, null));
            try store.put_boot(&.{
                .l1_head = l1_head,
                .agreed_l2_output_root = agreed_root,
                .claimed_l2_output_root = claimed_root,
                .claimed_l2_block_number = claimed_block,
                .chain_id = native.config.chain_id,
                .rollup_config_json = native.config.rollup_config_json,
                .l1_config_json = native.config.l1_config_json,
                .intermediate_block_interval = range.intermediate_block_interval,
                .schedule_block_number = range.schedule_l2_block_number,
            });
        }

        fn encode_archive(native: *Self, witness: *const host.Store) BackendError![]const u8 {
            const size = host.archive.encoded_bytes(witness);
            if (size > native.archive_buffer.len) return error.Failed;
            const keys = native.allocator.alloc([32]u8, witness.count()) catch return error.Failed;
            defer native.allocator.free(keys);
            var writer: std.Io.Writer = .fixed(native.archive_buffer);
            host.archive.encode(witness, keys, &writer) catch return error.Failed;
            return writer.buffered();
        }
    };
}
