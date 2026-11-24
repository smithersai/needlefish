//! Shared fuzz helpers.
//!
//! One fuzz body serves two drivers:
//!   - `std.testing.fuzz` (coverage guided, `zig build test --fuzz -Dtest-shards=false`), and
//!   - a seeded deterministic driver that runs on every `zig build test`, feeding
//!     `std.testing.Smith` from `Prng` bytes so a failure reproduces from
//!     `(seed, iteration)` alone.
//!
//! Usage:
//!   test "rlp: fuzz decode" {
//!       try fuzz.run({}, fuzz_decode, .{});
//!   }
//!   fn fuzz_decode(_: void, smith: *std.testing.Smith) !void { ... }
//!
//! Byte-oriented decoders can use `run_bytes` with a `fn (ctx, []const u8) !void`.
//!
//! Each input owns its memory. Under `--fuzz`, `std.testing.fuzz` gives every input a fresh
//! `std.testing.allocator` and fails the input on any allocation still live at its end, so
//! a context (a test harness, say) must not keep testing-allocator memory from one input to
//! the next. The seeded driver checks every input the same way.

const std = @import("std");
const builtin = @import("builtin");
const assert = std.debug.assert;
const Smith = std.testing.Smith;
const Prng = @import("testing/prng").Prng;
const build_options = @import("build_options");

/// `-Dfuzz-soak=N` multiplies every seeded run's iterations and `-Dfuzz-seed=S` moves it
/// to fresh inputs: `just fuzz-soak` explores far past the per-commit budget. The
/// defaults (1, 0) leave ordinary `zig build test` runs unchanged.
pub const soak_factor: u32 = build_options.fuzz_soak;
pub const soak_seed: u64 = build_options.fuzz_seed;

comptime {
    assert(soak_factor >= 1);
}

/// Largest input the seeded driver generates. Inputs live on the stack.
pub const input_size_limit: u32 = 16 * 1024;

pub const Options = struct {
    /// Base seed. Each iteration derives its own seed from `(seed, iteration)`.
    seed: u64 = 0x6e65_6564_6c65_6669,
    /// Seeded iterations per `zig build test` run. Keep cheap: the suite runs often.
    iterations: u32 = 256,
    /// Maximum generated input size in bytes, at most `input_size_limit`.
    input_size_max: u32 = 1024,
    /// Extra inputs fed to both drivers first (regressions, interesting cases).
    corpus: []const []const u8 = &.{},
    /// Also register the body with `std.testing.fuzz` (coverage guided).
    coverage: bool = true,
};

/// Seed for one iteration. Exposed so a failure report can be replayed exactly.
pub fn iteration_seed(seed: u64, iteration: u32) u64 {
    var hasher = std.hash.Wyhash.init(seed);
    hasher.update(std.mem.asBytes(&iteration));
    return hasher.final();
}

/// Run `test_one` under the seeded driver, then under `std.testing.fuzz`. The instrumented
/// build `--fuzz` makes (`builtin.fuzz`) skips the seeded driver: the fuzzer runs the test
/// again for every batch of inputs, and `--fuzz` already ran the uninstrumented build's tests,
/// seeded driver and `-Dfuzz-soak` included, before fuzzing.
pub fn run(
    context: anytype,
    comptime test_one: fn (@TypeOf(context), *Smith) anyerror!void,
    options: Options,
) !void {
    if (!builtin.fuzz) try run_seeded(context, test_one, options);
    if (options.coverage) {
        try std.testing.fuzz(context, test_one, .{ .corpus = options.corpus });
    }
}

/// Deterministic driver only. Runs the corpus, then `options.iterations` random inputs.
pub fn run_seeded(
    context: anytype,
    comptime test_one: fn (@TypeOf(context), *Smith) anyerror!void,
    options: Options,
) !void {
    assert(options.input_size_max <= input_size_limit);
    assert(options.iterations > 0 or options.corpus.len > 0);

    for (options.corpus, 0..) |input, index| {
        var smith: Smith = .{ .in = input };
        run_input(context, test_one, &smith, true) catch |err| {
            std.debug.print(
                "fuzz: failure corpus={d} input_len={d} error={s}\n",
                .{ index, input.len, @errorName(err) },
            );
            if (err == error.FuzzInputLeaked) panic_leaked();
            return err;
        };
    }

    var buffer: [input_size_limit]u8 = undefined;
    const iterations = std.math.mul(u32, options.iterations, soak_factor) catch
        std.math.maxInt(u32);
    const base_seed = options.seed ^ soak_seed;
    assert(iterations >= options.iterations);
    var iteration: u32 = 0;
    while (iteration < iterations) : (iteration += 1) {
        const seed = iteration_seed(base_seed, iteration);
        const input = generate(&buffer, seed, options.input_size_max);
        assert(input.len <= options.input_size_max);
        var smith: Smith = .{ .in = input };
        run_input(context, test_one, &smith, true) catch |err| {
            std.debug.print(
                "fuzz: failure seed=0x{x} iteration={d} input_len={d} error={s}\n",
                .{ base_seed, iteration, input.len, @errorName(err) },
            );
            if (err == error.FuzzInputLeaked) panic_leaked();
            return err;
        };
    }
}

/// The allocator options `std.testing.fuzz` gives each coverage-guided input
/// (lib/compiler/test_runner.zig). The canary differs from the test runner's, so freeing
/// memory allocated before the input panics as a foreign free, as it does under `--fuzz`.
const input_allocator_options: std.heap.SafeAllocator.Options = .{
    .canary = 0xcacce5e0,
    .check_write_after_free = true,
};

/// A leak check has freed the input's memory, which the context may still point at: returning
/// would let the test tear the context down through dangling pointers and crash there, far from
/// the cause. `std.testing.fuzz` exits the process at this point.
fn panic_leaked() noreturn {
    @panic("fuzz: the input kept std.testing.allocator memory past its end; free each " ++
        "input's allocations before it returns (src/testing/fuzz.zig)");
}

/// Run one input under a fresh `std.testing.allocator`, as `std.testing.fuzz` runs each
/// coverage-guided input, and fail with `error.FuzzInputLeaked` when the input leaves any
/// allocation live. `log_leaks` logs each leak with its allocation stack (an error log, which
/// fails the test by itself). After an error the input's allocator stays mapped, unchecked,
/// like `std.testing.fuzz`, which exits the process there.
fn run_input(
    context: anytype,
    comptime test_one: fn (@TypeOf(context), *Smith) anyerror!void,
    smith: *Smith,
    log_leaks: bool,
) !void {
    const outer = std.testing.allocator_instance;
    assert(outer.canary != input_allocator_options.canary); // Inputs do not nest.
    defer std.testing.allocator_instance = outer;
    std.testing.allocator_instance = .init(std.heap.page_allocator, input_allocator_options);
    try test_one(context, smith);
    // The input must not swap the testing allocator itself.
    assert(std.testing.allocator_instance.canary == input_allocator_options.canary);
    if (std.testing.allocator_instance.deinitLog(log_leaks) > 0) return error.FuzzInputLeaked;
}

/// Byte-oriented convenience: `test_bytes` receives the raw fuzz input.
pub fn run_bytes(
    context: anytype,
    comptime test_bytes: fn (@TypeOf(context), []const u8) anyerror!void,
    options: Options,
) !void {
    const Context = @TypeOf(context);
    const Adapter = struct {
        fn test_one(inner: Context, smith: *Smith) anyerror!void {
            var buffer: [input_size_limit]u8 = undefined;
            const len = smith.slice(&buffer);
            assert(len <= buffer.len);
            try test_bytes(inner, buffer[0..len]);
        }
    };
    try run(context, Adapter.test_one, options);
}

/// Fill a prefix of `buffer` with seeded bytes. Lengths skew small: most decoder
/// bugs live in short inputs, and short inputs keep the suite fast.
///
/// `Smith` replays `.in` bytes literally: a value reads 8 little-endian bytes and
/// falls back to its minimum when they miss the range, `eos` ends on any nonzero
/// byte, and a slice reads a u32 length. Uniform random bytes therefore yield
/// minimums, instant loop ends, and empty slices. Three quarters of the inputs are words
/// instead: little-endian u64s with skewed-small values, which land in typical ranges
/// and read as mostly-zero `eos` bytes.
fn generate(buffer: []u8, seed: u64, input_size_max: u32) []const u8 {
    assert(input_size_max <= buffer.len);
    var prng = Prng.init(seed);
    const len_max: u64 = switch (prng.below(4)) {
        0 => @min(input_size_max, 16),
        1 => @min(input_size_max, 128),
        else => input_size_max,
    };
    const len: usize = @intCast(prng.range(0, len_max));
    const input = buffer[0..len];
    if (prng.chance(3, 4)) {
        generate_words(&prng, input);
        return input;
    }
    // Mix fully random bytes with structured fills that decoders find interesting.
    switch (prng.below(4)) {
        0 => @memset(input, 0),
        1 => @memset(input, 0xff),
        else => prng.fill(input),
    }
    if (len > 0 and prng.chance(1, 2)) {
        const flips = prng.range(1, 8);
        var flip: u64 = 0;
        while (flip < flips) : (flip += 1) {
            input[@intCast(prng.below(len))] = prng.int(u8);
        }
    }
    return input;
}

fn generate_words(prng: *Prng, input: []u8) void {
    var offset: usize = 0;
    while (offset < input.len) : (offset += 8) {
        // Spread over small ranges: booleans and `u1`s accept only 0 or 1, enum-like
        // selectors a handful of values.
        const word: u64 = switch (prng.below(8)) {
            0 => prng.below(2),
            1 => prng.below(4),
            2 => prng.below(8),
            3 => prng.below(16),
            4 => prng.below(64),
            5 => prng.below(256),
            6 => prng.below(1 << 16),
            else => prng.int(u64),
        };
        var bytes: [8]u8 = undefined;
        std.mem.writeInt(u64, &bytes, word, .little);
        const length = @min(8, input.len - offset);
        @memcpy(input[offset..][0..length], bytes[0..length]);
    }
    assert(offset >= input.len);
}

const Spread = struct {
    seen: [21]bool = @splat(false),
    true_count: u32 = 0,
    loop_max: u32 = 0,
    slice_max: u32 = 0,
};

fn spread_values(spread: *Spread, smith: *Smith) anyerror!void {
    spread.seen[smith.valueRangeAtMost(u32, 0, 20)] = true;
    spread.true_count += @intFromBool(smith.value(bool));
    var loop: u32 = 0;
    while (loop < 64 and !smith.eosWeightedSimple(7, 1)) loop += 1;
    spread.loop_max = @max(spread.loop_max, loop);
    var buffer: [32]u8 = undefined;
    spread.slice_max = @max(spread.slice_max, smith.slice(&buffer));
}

test "fuzz: seeded inputs drive Smith past its minimums" {
    var spread: Spread = .{};
    try run_seeded(&spread, spread_values, .{ .iterations = 512 });
    var distinct: u32 = 0;
    for (spread.seen) |seen| distinct += @intFromBool(seen);
    try std.testing.expect(distinct >= 15);
    try std.testing.expect(spread.true_count >= 16);
    try std.testing.expect(spread.loop_max >= 4);
    try std.testing.expect(spread.slice_max >= 4);
}

test "fuzz: iteration seeds are distinct and stable" {
    try std.testing.expectEqual(iteration_seed(1, 0), iteration_seed(1, 0));
    try std.testing.expect(iteration_seed(1, 0) != iteration_seed(1, 1));
    try std.testing.expect(iteration_seed(1, 0) != iteration_seed(2, 0));
}

test "fuzz: generate respects bounds" {
    var buffer: [256]u8 = undefined;
    var iteration: u32 = 0;
    while (iteration < 1000) : (iteration += 1) {
        const input = generate(&buffer, iteration, 200);
        try std.testing.expect(input.len <= 200);
    }
}

const Counter = struct { calls: u32 = 0, bytes: u64 = 0 };

fn count_bytes(counter: *Counter, input: []const u8) anyerror!void {
    counter.calls += 1;
    counter.bytes += input.len;
}

test "fuzz: seeded driver is deterministic" {
    var a: Counter = .{};
    var b: Counter = .{};
    const options: Options = .{ .iterations = 64, .coverage = false, .corpus = &.{"abc"} };
    try run_bytes(&a, count_bytes, options);
    try run_bytes(&b, count_bytes, options);
    try std.testing.expectEqual(1 + 64 * soak_factor, a.calls);
    try std.testing.expectEqual(a.bytes, b.bytes);
}

const Keeper = struct {
    kept: ?[]u8 = null,
    inputs: u32 = 0,
};

fn keep_first(keeper: *Keeper, _: *Smith) anyerror!void {
    keeper.inputs += 1;
    if (keeper.kept == null) keeper.kept = try std.testing.allocator.alloc(u8, 53);
}

fn allocate_and_free(keeper: *Keeper, _: *Smith) anyerror!void {
    keeper.inputs += 1;
    const bytes = try std.testing.allocator.alloc(u8, 53);
    std.testing.allocator.free(bytes);
}

test "fuzz: an input that keeps testing-allocator memory past its end fails" {
    var keeper: Keeper = .{};
    var smith: Smith = .{ .in = "" };
    const result = run_input(&keeper, keep_first, &smith, false);
    try std.testing.expectError(error.FuzzInputLeaked, result);
    // The input's allocator freed what it kept: `keeper.kept` dangles and is never read.
    try std.testing.expect(keeper.kept != null);
    try std.testing.expectEqual(@as(u32, 1), keeper.inputs);
}

test "fuzz: seeded inputs that free their memory pass beside outer allocations" {
    const outer = try std.testing.allocator.alloc(u8, 8);
    defer std.testing.allocator.free(outer);
    var keeper: Keeper = .{};
    try run_seeded(&keeper, allocate_and_free, .{ .iterations = 8, .corpus = &.{"abc"} });
    try std.testing.expectEqual(1 + 8 * soak_factor, keeper.inputs);
}

fn smith_values(_: void, smith: *Smith) anyerror!void {
    const x = smith.valueRangeAtMost(u16, 3, 9);
    try std.testing.expect(x >= 3 and x <= 9);
}

test "fuzz: run with coverage target" {
    try run({}, smith_values, .{ .iterations = 32 });
}
