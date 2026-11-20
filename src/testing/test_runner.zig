//! Test runner that runs one shard of a test binary.
//!
//! This is Zig 0.17.1's default test runner (lib/compiler/test_runner.zig; MIT License,
//! Copyright (c) Zig contributors) plus one argument, `--shard=K/N`: run only the tests whose
//! full name hashes to shard K of N (FNV-1a 64 of the name, modulo N). build.zig compiles a
//! slow package's tests once and runs that binary N times in parallel, one shard each, so the
//! run costs its slowest shard instead of the sum of every test. A test's shard depends on
//! nothing but its name and N: it is the same on every machine, and a new test lands in a
//! shard by itself. Each shard tells the build runner only its own tests, so `--summary`
//! counts every test once and a failure names its test. Without `--shard` every test runs,
//! as with the default runner. Fuzzing (`zig build test --fuzz`) names its tests and is
//! unchanged.

const builtin = @import("builtin");
const std = @import("std");
const Io = std.Io;
const testing = std.testing;
const assert = std.debug.assert;
const panic = std.debug.panic;
const fuzz_abi = std.Build.abi.fuzz;

pub const std_options: std.Options = .{
    .logFn = log,
};

comptime {
    // The default runner falls back to a protocol-less loop on these backends; a shard needs
    // the build runner's protocol to report its own test count.
    switch (builtin.zig_backend) {
        .stage2_aarch64, .stage2_loongarch, .stage2_powerpc, .stage2_riscv64 => @compileError(
            "the shard test runner needs a backend that runs the default runner's server mode",
        ),
        else => {},
    }
}

var log_err_count: usize = 0;
var fba: std.heap.FixedBufferAllocator = .init(&fba_buffer);
var fba_buffer: [8192]u8 = undefined;
var stdin_buffer: [4096]u8 = undefined;
var stdout_buffer: [4096]u8 = undefined;
var stdin_reader: Io.File.Reader = undefined;
var stdout_writer: Io.File.Writer = undefined;
const runner_threaded_io: Io = Io.Threaded.global_single_threaded.io();

/// Indexes into `builtin.test_functions` of this run's tests, in binary order; allocated once
/// at startup.
var shard_tests: []const u32 = &.{};

/// Shard `index` of `count`: the tests whose name hashes to `index` modulo `count`.
pub const Shard = struct {
    index: u32,
    count: u32,

    pub const all: Shard = .{ .index = 0, .count = 1 };

    /// Parses "K/N" (decimal, N >= 1, K < N).
    pub fn parse(text: []const u8) ?Shard {
        const slash = std.mem.indexOfScalar(u8, text, '/') orelse return null;
        const index = std.fmt.parseUnsigned(u32, text[0..slash], 10) catch return null;
        const count = std.fmt.parseUnsigned(u32, text[slash + 1 ..], 10) catch return null;
        if (count == 0 or index >= count) return null;
        const shard: Shard = .{ .index = index, .count = count };
        assert(shard.index < shard.count);
        return shard;
    }

    /// True when the test named `name` runs in this shard.
    pub fn contains(shard: Shard, name: []const u8) bool {
        assert(shard.count >= 1);
        assert(shard.index < shard.count);
        assert(name.len > 0);
        return std.hash.Fnv1a_64.hash(name) % shard.count == shard.index;
    }
};

/// Fills `shard_tests` with the tests of `shard`.
fn select(shard: Shard) void {
    const test_functions = builtin.test_functions;
    const buffer = std.heap.page_allocator.alloc(u32, test_functions.len) catch
        @panic("unable to allocate the shard's test list");
    var count: u32 = 0;
    for (test_functions, 0..) |test_fn, index| {
        if (!shard.contains(test_fn.name)) continue;
        buffer[count] = @intCast(index);
        count += 1;
    }
    assert(count <= test_functions.len);
    if (shard.count == 1) assert(count == test_functions.len);
    shard_tests = buffer[0..count];
}

pub fn main(init: std.process.Init.Minimal) void {
    @disableInstrumentation();
    const args = init.args.toSlice(fba.allocator()) catch |err|
        panic("unable to parse command line args: {t}", .{err});

    var listen = false;
    var opt_cache_dir: ?[]const u8 = null;
    var shard: Shard = .all;
    for (args[1..]) |arg| {
        if (std.mem.eql(u8, arg, "--listen=-")) {
            listen = true;
        } else if (std.mem.startsWith(u8, arg, "--seed=")) {
            testing.random_seed = std.fmt.parseUnsigned(u32, arg["--seed=".len..], 0) catch
                @panic("unable to parse --seed command line argument");
        } else if (std.mem.startsWith(u8, arg, "--cache-dir")) {
            opt_cache_dir = arg["--cache-dir=".len..];
        } else if (std.mem.startsWith(u8, arg, "--shard=")) {
            shard = Shard.parse(arg["--shard=".len..]) orelse
                panic("invalid argument {s}: expected --shard=K/N with K < N", .{arg});
        } else {
            panic("unrecognized command line argument: {s}", .{arg});
        }
    }
    select(shard);

    if (builtin.fuzz) {
        const cache_dir = opt_cache_dir orelse @panic("missing --cache-dir=[path] argument");
        fuzz_abi.fuzzer_init(.fromSlice(cache_dir));
    }

    if (listen) {
        return main_server(init) catch |err| panic("internal test runner failure: {t}", .{err});
    } else {
        return main_terminal(init);
    }
}

fn main_server(init: std.process.Init.Minimal) !void {
    @disableInstrumentation();
    stdin_reader = .initStreaming(.stdin(), runner_threaded_io, &stdin_buffer);
    stdout_writer = .initStreaming(.stdout(), runner_threaded_io, &stdout_buffer);
    var server: std.zig.Server = .{
        .in = &stdin_reader.interface,
        .out = &stdout_writer.interface,
    };
    try server.serveStringMessage(.zig_version, builtin.zig_version_string);

    while (true) {
        const hdr = try server.receiveMessage();
        switch (hdr.tag) {
            .exit => return std.process.exit(0),
            .query_test_metadata => try serve_test_metadata(&server),
            .run_test => try serve_run_test(&server, init),
            .start_fuzzing => {
                // This ensures that this code won't be analyzed and hence reference fuzzer
                // symbols since they are not present.
                if (!builtin.fuzz) unreachable;
                try serve_start_fuzzing(&server, init);
            },
            else => {
                std.debug.print("unsupported message: {x}\n", .{@backingInt(hdr.tag)});
                std.process.exit(1);
            },
        }
    }
}

/// Tells the build runner this shard's tests; it then asks for each by its position here.
fn serve_test_metadata(server: *std.zig.Server) !void {
    var sa: std.heap.SafeAllocator = .init(std.heap.page_allocator, .{});
    defer if (sa.deinit() != 0) @panic("internal test runner memory leak");
    const gpa = sa.allocator();

    var string_bytes: std.ArrayList(u8) = .empty;
    defer string_bytes.deinit(gpa);
    try string_bytes.append(gpa, 0); // Reserve 0 for null.

    const names = try gpa.alloc(u32, shard_tests.len);
    defer gpa.free(names);
    const expected_panic_msgs = try gpa.alloc(u32, shard_tests.len);
    defer gpa.free(expected_panic_msgs);

    for (shard_tests, names, expected_panic_msgs) |test_index, *name, *expected_panic_msg| {
        const test_name = builtin.test_functions[test_index].name;
        name.* = @intCast(string_bytes.items.len);
        try string_bytes.ensureUnusedCapacity(gpa, test_name.len + 1);
        string_bytes.appendSliceAssumeCapacity(test_name);
        string_bytes.appendAssumeCapacity(0);
        expected_panic_msg.* = 0;
    }
    assert(string_bytes.items.len >= 1 + shard_tests.len);

    try server.serveTestMetadata(.{
        .names = names,
        .expected_panic_msgs = expected_panic_msgs,
        .string_bytes = string_bytes.items,
    });
}

fn serve_run_test(server: *std.zig.Server, init: std.process.Init.Minimal) !void {
    testing.environ = init.environ;
    testing.allocator_instance = .init(std.heap.page_allocator, .{
        .canary = 0xc3a701ba,
        .check_write_after_free = true,
    });
    testing.io_instance = .init(testing.allocator, .{
        .argv0 = .init(init.args),
        .environ = init.environ,
    });
    log_err_count = 0;
    // The index is a position in this shard's metadata, not in `builtin.test_functions`.
    const index = try server.receiveBody_u32();
    if (index >= shard_tests.len) panic("run_test index {d} outside the shard", .{index});
    const test_fn = builtin.test_functions[shard_tests[index]];
    is_fuzz_test = false;

    // let the build server know we're starting the test now
    try server.serveStringMessage(.test_started, &.{});

    const TestResults = std.zig.Server.Message.TestResults;
    const status: TestResults.Status = if (test_fn.func()) |v| s: {
        v;
        break :s .pass;
    } else |err| switch (err) {
        error.SkipZigTest => .skip,
        else => s: {
            if (@errorReturnTrace()) |trace| {
                std.debug.dumpErrorReturnTrace(trace);
            }
            break :s .fail;
        },
    };
    testing.io_instance.deinit();
    const leak_count = testing.allocator_instance.deinit();
    try server.serveTestResults(.{
        .index = index,
        .flags = .{
            .status = status,
            .fuzz = is_fuzz_test,
            .log_err_count = std.math.lossyCast(
                @FieldType(TestResults.Flags, "log_err_count"),
                log_err_count,
            ),
            .leak_count = std.math.lossyCast(
                @FieldType(TestResults.Flags, "leak_count"),
                leak_count,
            ),
        },
    });
}

fn serve_start_fuzzing(server: *std.zig.Server, init: std.process.Init.Minimal) !void {
    var gpa_instance: std.heap.SafeAllocator = .init(std.heap.page_allocator, .{});
    defer if (gpa_instance.deinit() != 0) {
        @panic("internal test runner memory leak");
    };
    const gpa = gpa_instance.allocator();
    var io_instance: Io.Threaded = .init(gpa, .{
        .argv0 = .init(init.args),
        .environ = init.environ,
    });
    defer io_instance.deinit();

    const mode: fuzz_abi.LimitKind = @fromBackingInt(@intCast(try server.receiveBody_u8()));
    const amount_or_instance = try server.receiveBody_u64();
    const main_instance = mode == .iterations or amount_or_instance == 0;

    if (main_instance) {
        const coverage = fuzz_abi.fuzzer_coverage();
        try server.serveCoverageIdMessage(
            coverage.id,
            coverage.runs,
            coverage.unique,
            coverage.seen,
        );
    }

    const n_tests: u32 = try server.receiveBody_u32();
    const test_indexes = try gpa.alloc(u32, n_tests);
    defer gpa.free(test_indexes);
    fuzz_runner = .{
        .indexes = test_indexes,
        .server = server,
        .gpa = gpa,
        .threaded_io = &io_instance,
        .input_poller = undefined,
    };
    try receive_fuzz_tests(server, gpa, test_indexes, main_instance);

    fuzz_abi.fuzzer_main(n_tests, testing.random_seed, mode, amount_or_instance);

    assert(mode != .forever);
    std.process.exit(0);
}

/// Fuzzing names its tests: they are found among every test of the binary, not the shard.
fn receive_fuzz_tests(
    server: *std.zig.Server,
    gpa: std.mem.Allocator,
    test_indexes: []u32,
    main_instance: bool,
) !void {
    var large_name_buf: std.ArrayList(u8) = .empty;
    defer large_name_buf.deinit(gpa);
    for (test_indexes) |*i| {
        const name_len = try server.receiveBody_u32();
        const name = if (name_len <= server.in.buffer.len)
            try server.in.take(name_len)
        else large_name: {
            try large_name_buf.resize(gpa, name_len);
            try server.in.readSliceAll(large_name_buf.items);
            break :large_name large_name_buf.items;
        };

        for (0.., builtin.test_functions) |test_i, test_fn| {
            if (std.mem.eql(u8, name, test_fn.name)) {
                i.* = @intCast(test_i);
                break;
            }
        } else {
            panic("fuzz test {s} no longer exists", .{name});
        }

        if (main_instance) {
            const relocated_entry_addr = @intFromPtr(builtin.test_functions[i.*].func);
            const entry_addr = fuzz_abi.fuzzer_unslide_address(relocated_entry_addr);
            try server.serveU64Message(.fuzz_start_addr, entry_addr);
        }
    }
}

const Counts = struct {
    ok: u32 = 0,
    skip: u32 = 0,
    fail: u32 = 0,
    fuzz: u32 = 0,
    leaks: u32 = 0,
};

fn main_terminal(init: std.process.Init.Minimal) void {
    @disableInstrumentation();
    if (builtin.fuzz) @panic("fuzz test requires server");

    var counts: Counts = .{};
    const root_node = std.Progress.start(runner_threaded_io, .{
        .root_name = "Test",
        .estimated_total_items = shard_tests.len,
    });
    const have_tty = Io.File.stderr().isTty(runner_threaded_io) catch false;

    for (shard_tests, 0..) |test_index, position| {
        run_terminal_test(init, &counts, root_node, have_tty, test_index, position);
    }
    root_node.end();
    assert(counts.ok + counts.skip + counts.fail == shard_tests.len);
    if (counts.ok == shard_tests.len) {
        std.debug.print("All {d} tests passed.\n", .{counts.ok});
    } else {
        std.debug.print("{d} passed; {d} skipped; {d} failed.\n", .{
            counts.ok, counts.skip, counts.fail,
        });
    }
    if (log_err_count != 0) {
        std.debug.print("{d} errors were logged.\n", .{log_err_count});
    }
    if (counts.leaks != 0) {
        std.debug.print("{d} tests leaked memory.\n", .{counts.leaks});
    }
    if (counts.fuzz != 0) {
        std.debug.print("{d} fuzz tests found.\n", .{counts.fuzz});
    }
    if (counts.leaks != 0 or log_err_count != 0 or counts.fail != 0) {
        std.process.exit(1);
    }
}

fn run_terminal_test(
    init: std.process.Init.Minimal,
    counts: *Counts,
    root_node: std.Progress.Node,
    have_tty: bool,
    test_index: u32,
    position: usize,
) void {
    const test_fn = builtin.test_functions[test_index];
    const total = shard_tests.len;
    assert(position < total);
    testing.allocator_instance = .init(std.heap.page_allocator, .{
        .canary = 0xc3a701ba,
        .check_write_after_free = true,
    });
    testing.io_instance = .init(testing.allocator, .{
        .argv0 = .init(init.args),
        .environ = init.environ,
    });
    defer {
        testing.io_instance.deinit();
        if (testing.allocator_instance.deinit() != 0) counts.leaks += 1;
    }
    testing.log_level = .warn;
    testing.environ = init.environ;

    const test_node = root_node.start(test_fn.name, 0);
    defer test_node.end();
    if (!have_tty) std.debug.print("{d}/{d} {s}...", .{ position + 1, total, test_fn.name });
    is_fuzz_test = false;
    defer counts.fuzz += @intFromBool(is_fuzz_test);
    if (test_fn.func()) |_| {
        counts.ok += 1;
        if (!have_tty) std.debug.print("OK\n", .{});
    } else |err| switch (err) {
        error.SkipZigTest => {
            counts.skip += 1;
            if (have_tty) {
                std.debug.print("{d}/{d} {s}...SKIP\n", .{ position + 1, total, test_fn.name });
            } else {
                std.debug.print("SKIP\n", .{});
            }
        },
        else => {
            counts.fail += 1;
            if (have_tty) {
                std.debug.print("{d}/{d} {s}...FAIL ({t})\n", .{
                    position + 1, total, test_fn.name, err,
                });
            } else {
                std.debug.print("FAIL ({t})\n", .{err});
            }
            if (@errorReturnTrace()) |trace| {
                std.debug.dumpErrorReturnTrace(trace);
            }
        },
    }
}

pub fn log(
    comptime message_level: std.log.Level,
    comptime scope: @EnumLiteral(),
    comptime format: []const u8,
    args: anytype,
) void {
    @disableInstrumentation();
    if (@backingInt(message_level) <= @backingInt(std.log.Level.err)) {
        log_err_count +|= 1;
    }
    if (@backingInt(message_level) <= @backingInt(testing.log_level)) {
        std.debug.print(
            "[" ++ @tagName(scope) ++ "] (" ++ @tagName(message_level) ++ "): " ++ format ++ "\n",
            args,
        );
    }
}

var is_fuzz_test: bool = undefined;
var fuzz_runner: if (builtin.fuzz) struct {
    indexes: []u32,
    server: *std.zig.Server,
    gpa: std.mem.Allocator,
    threaded_io: *Io.Threaded,
    input_poller: Io.Future(Io.Cancelable!void),

    comptime {
        assert(builtin.fuzz); // `fuzz_runner` was analyzed in non-fuzzing compilation
    }

    export fn runner_test_run(i: u32) void {
        @disableInstrumentation();

        fuzz_runner.server.serveU32Message(.fuzz_test_change, i) catch |e| switch (e) {
            error.WriteFailed => panic("failed to write to stdout: {t}", .{stdout_writer.err.?}),
        };

        testing.allocator_instance = .init(std.heap.page_allocator, .{
            .canary = 0xc3a701ba,
            .check_write_after_free = true,
        });
        defer if (testing.allocator_instance.deinit() != 0) std.process.exit(1);
        is_fuzz_test = false;

        testing.io_instance = .init(testing.allocator, .{
            .argv0 = fuzz_runner.threaded_io.argv0,
            .environ = fuzz_runner.threaded_io.environ.process_environ,
        });
        defer testing.io_instance.deinit();

        builtin.test_functions[fuzz_runner.indexes[i]].func() catch |err| switch (err) {
            error.SkipZigTest => return,
            else => {
                if (@errorReturnTrace()) |trace| {
                    std.debug.dumpErrorReturnTrace(trace);
                }
                std.debug.print("failed with error.{t}\n", .{err});
                std.process.exit(1);
            },
        };

        if (!is_fuzz_test) @panic("missed call to std.testing.fuzz");
        if (log_err_count != 0) @panic("error logs detected");
    }

    export fn runner_test_name(i: u32) fuzz_abi.Slice {
        @disableInstrumentation();
        return .fromSlice(builtin.test_functions[fuzz_runner.indexes[i]].name);
    }

    export fn runner_broadcast_input(test_i: u32, bytes_slice: fuzz_abi.Slice) void {
        @disableInstrumentation();
        const bytes = bytes_slice.toSlice();
        fuzz_runner.server.serveBroadcastFuzzInputMessage(test_i, bytes) catch |e| switch (e) {
            error.WriteFailed => panic("failed to write to stdout: {t}", .{stdout_writer.err.?}),
        };
    }

    export fn runner_start_input_poller() void {
        @disableInstrumentation();
        const io = fuzz_runner.threaded_io.io();
        const future = io.concurrent(inputPoller, .{}) catch |e| switch (e) {
            error.ConcurrencyUnavailable => @panic("failed to spawn concurrent fuzz input poller"),
        };
        fuzz_runner.input_poller = future;
    }

    export fn runner_stop_input_poller() void {
        @disableInstrumentation();
        const io = fuzz_runner.threaded_io.io();
        assert(fuzz_runner.input_poller.cancel(io) == error.Canceled);
    }

    export fn runner_futex_wait(ptr: *const u32, expected: u32) bool {
        @disableInstrumentation();
        const io = fuzz_runner.threaded_io.io();
        return io.futexWait(u32, ptr, expected) == error.Canceled;
    }

    export fn runner_futex_wake(ptr: *const u32, waiters: u32) void {
        @disableInstrumentation();
        const io = fuzz_runner.threaded_io.io();
        io.futexWake(u32, ptr, waiters);
    }

    fn inputPoller() Io.Cancelable!void {
        @disableInstrumentation();
        switch (inputPollerInner()) {
            error.Canceled => |e| return e,
            error.ReadFailed => {
                if (stdin_reader.err.? == error.Canceled) return error.Canceled;
                panic("failed to read from stdin: {t}", .{stdin_reader.err.?});
            },
            error.EndOfStream => @panic("unexpected end of stdin"),
        }
    }

    fn inputPollerInner() (Io.Cancelable || Io.Reader.Error) {
        @disableInstrumentation();
        const server = fuzz_runner.server;
        var large_bytes_list: std.ArrayList(u8) = .empty;
        defer large_bytes_list.deinit(fuzz_runner.gpa);
        while (true) {
            const hdr = try server.receiveMessage();
            if (hdr.tag != .new_fuzz_input) {
                panic("unexpected message: {x}\n", .{@backingInt(hdr.tag)});
            }
            const test_i = try server.receiveBody_u32();
            const input_len = hdr.bytes_len - 4;
            const bytes = if (input_len <= server.in.buffer.len)
                try server.in.take(input_len)
            else bytes: {
                large_bytes_list.resize(fuzz_runner.gpa, @intCast(input_len)) catch @panic("OOM");
                try server.in.readSliceAll(large_bytes_list.items);
                break :bytes large_bytes_list.items;
            };
            if (fuzz_abi.fuzzer_receive_input(test_i, .fromSlice(bytes))) {
                return error.Canceled;
            }
        }
    }
} else void = undefined;

/// `std.testing.fuzz` calls this through `@import("root")`.
pub fn fuzz(
    context: anytype,
    comptime testOne: fn (context: @TypeOf(context), *std.testing.Smith) anyerror!void,
    options: testing.FuzzInputOptions,
) anyerror!void {
    // Prevent this function from confusing the fuzzer by omitting its own code
    // coverage from being considered.
    @disableInstrumentation();

    // Smoke test to ensure the test did not use conditional compilation to
    // contradict itself by making it not actually be a fuzz test when the test
    // is built in fuzz mode.
    is_fuzz_test = true;

    // Ensure no test failure occurred before starting fuzzing.
    if (log_err_count != 0) @panic("error logs detected");

    if (builtin.fuzz) {
        const global = FuzzGlobal(@TypeOf(context), testOne);
        // Preserve the calling test's allocator state
        const prev_allocator_state = testing.allocator_instance;
        defer testing.allocator_instance = prev_allocator_state;

        global.ctx = context;
        fuzz_abi.fuzzer_set_test(&global.test_one);
        for (options.corpus) |elem|
            fuzz_abi.fuzzer_new_input(.fromSlice(elem));
        fuzz_abi.fuzzer_start_test();
        return;
    }

    // When the unit test executable is not built in fuzz mode, only run the
    // provided corpus.
    for (options.corpus) |input| {
        var smith: testing.Smith = .{ .in = input };
        try testOne(context, &smith);
    }

    // In case there is no provided corpus, also use an empty
    // string as a smoke test.
    var smith: testing.Smith = .{ .in = "" };
    try testOne(context, &smith);
}

/// libfuzzer is in a separate compilation unit so that its own code can be excluded from code
/// coverage instrumentation. It needs a function pointer it can call for checking exactly one
/// input. Inside this function we do our standard unit test checks such as memory leaks, and
/// interaction with error logs.
fn FuzzGlobal(
    comptime Context: type,
    comptime testOne: fn (context: Context, *std.testing.Smith) anyerror!void,
) type {
    return struct {
        var ctx: Context = undefined;

        fn test_one() callconv(.c) bool {
            @disableInstrumentation();
            testing.allocator_instance = .init(std.heap.page_allocator, .{
                .canary = 0xcacce5e0,
                .check_write_after_free = true,
            });
            defer if (testing.allocator_instance.deinit() != 0) std.process.exit(1);
            log_err_count = 0;
            testOne(ctx, @constCast(&testing.Smith{ .in = null })) catch |err| switch (err) {
                error.SkipZigTest => return true,
                else => {
                    const stderr = std.debug.lockStderr(&.{}).terminal();
                    p: {
                        if (@errorReturnTrace()) |trace| {
                            std.debug.writeErrorReturnTrace(trace, stderr) catch break :p;
                        }
                        stderr.writer.print("failed with error.{t}\n", .{err}) catch break :p;
                    }
                    std.process.exit(1);
                },
            };
            if (log_err_count != 0) {
                const stderr = std.debug.lockStderr(&.{}).terminal();
                stderr.writer.print("error logs detected\n", .{}) catch {};
                std.process.exit(1);
            }
            return false;
        }
    };
}
