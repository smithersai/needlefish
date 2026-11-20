//! Simulation benchmark cases: one cluster seed per iteration.
//!
//! The harness is `testing/bench`; `bench_main.zig` lists `cases` with the other tables.

const std = @import("std");
const Case = @import("testing/bench").Case;
const swarm = @import("swarm.zig");

fn bench_sim_seed(iterations: u32) u64 {
    var accumulator: u64 = 0;
    var seed: u64 = 0;
    while (seed < iterations) : (seed += 1) {
        const config = swarm.config_for_seed(seed);
        const outcome = swarm.run_config(std.heap.page_allocator, seed, config);
        accumulator +%= @backingInt(outcome catch unreachable); // Allocation only.
    }
    return accumulator;
}

pub const cases = [_]Case{
    .{ .name = "sim/cluster seed (16 sim-minutes)", .iterations = 100, .run = bench_sim_seed },
};
