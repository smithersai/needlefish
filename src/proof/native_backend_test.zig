//! The prover service's native backend (`proof/prover_service` native_backend.zig) against
//! sources that never answer. The backend runs the fault-proof client, and with it the
//! EVM, so this test sits in the proof group, above the prover service and the client.

const std = @import("std");
const prover_service = @import("proof/prover_service");
const protocol = prover_service.protocol;
const host = @import("proof/host");
const Native = prover_service.native_backend.Native;

const testing = std.testing;
const io = @import("utilities/io");
const http = @import("utilities/http");

test "prover service native backend: unsupported kinds and unreachable sources fail cleanly" {
    const allocator = testing.allocator;
    var world = try io.SimWorld.init(allocator, .{ .seed = 3 });
    defer world.deinit();
    var sim_io: io.SimIo = undefined;
    try io.SimIo.init(&sim_io, allocator, &world, .{ .host = io.Address.ip4(.{ 10, 0, 0, 2 }, 0) });
    defer sim_io.deinit();
    const Backend = Native(io.SimIo);
    const nowhere = io.Address.ip4(.{ 10, 0, 0, 9 }, 8545);
    const http_options: http.client.Options = .{
        .send_buffer_size = 4096,
        .recv_buffer_size = 16384,
        .limits = .{ .head_size_max = 1024, .body_size_max = 8192 },
    };
    var l1: Backend.Source.Client = try .init(allocator, &sim_io, nowhere, http_options, 4096);
    defer l1.deinit();
    var l2: Backend.Source.Client = try .init(allocator, &sim_io, nowhere, http_options, 4096);
    defer l2.deinit();
    const Pump = struct {
        fn step(context: *anyopaque) anyerror!void {
            const sim_world: *io.SimWorld = @ptrCast(@alignCast(context));
            sim_world.run_for_ns(std.time.ns_per_ms);
        }
    };
    const pump: host.rpc_source.Pump = .{ .context = &world, .step = Pump.step };
    var source: Backend.Source = .init(allocator, &l1, &l2, pump, .{ .steps_max = 1000 });
    defer source.deinit();
    var native: Backend = try .init(allocator, &source, .{
        .rollup_config_json = "{}",
        .chain_id = 8453,
        // Never reached: the source fails before the client runs.
        .client_options = undefined,
        .proof_bytes_max = 1024,
    });
    defer native.deinit();
    const backend = native.backend();
    var job: protocol.ProofJob = .{
        .session_id = .from("s"),
        .status = .claimed,
        .request = .{ .session_id = .from("s"), .request = .{ .compressed = .{
            .start_block_number = 10,
            .number_of_blocks_to_prove = 2,
            .intermediate_block_interval = 1,
            .zk_vm = .sp1,
        } } },
        .attempt = 1,
        .created_at = .{ .seconds = 0 },
        .updated_at = .{ .seconds = 0 },
    };
    try backend.start(&job);
    try testing.expectError(error.Failed, backend.poll());
    try testing.expect(try backend.poll() == null);
    job.request.request = .{ .tee = undefined };
    try testing.expectError(error.Unsupported, backend.start(&job));
}
