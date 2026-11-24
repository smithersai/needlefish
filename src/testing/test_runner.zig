//! Test runner of every package's test binary while shards are on (the default;
//! `-Dtest-shards=false` gives every binary Zig's default runner instead).
//!
//! This is Zig 0.17.1's default test runner (lib/compiler/test_runner.zig; MIT License,
//! Copyright (c) Zig contributors) plus three changes. First, one argument, `--shard=K/N`: run
//! only the tests whose full name hashes to shard K of N (FNV-1a 64 of the name, modulo N).
//! build.zig compiles a slow package's tests once and runs that binary N times in parallel,
//! one shard each, so the run costs its slowest shard instead of the sum of every test. A
//! test's shard depends on nothing but its name and N: it is the same on every machine, and a
//! new test lands in a shard by itself. Each shard tells the build runner only its own tests,
//! so `--summary` counts every test once and a failure names its test. Without `--shard`
//! every test runs, as with the default runner: a package with one shard passes none.
//!
//! Second, a test that fails by returning an error prints `'<name>' failed with
//! error.<Name>` to stderr, then its error return trace if it has one. The default runner
//! prints only the trace, and the build runner shows a failed test's stderr, so a test binary
//! without debug info (no trace) reported only "failed without output".
//!
//! Third, a binary without debug info runs on `StderrIo`, not `std.Io.Threaded` (`lean_io`):
//! an empty ReleaseSafe test binary then compiles in 1.0 s instead of 3.5 s.
//!
//! Not fuzzing. The build runner keeps fuzzing state per run step (a rebuild in fuzz mode,
//! its workers, the cache files it recovers a crashing input from), and shards of one binary
//! would share that state and race on it. So this runner reports no test as a fuzz test:
//! `zig build test --fuzz` rebuilds and fuzzes nothing. To fuzz, build with
//! `-Dtest-shards=false`: every binary then runs once with the default runner, as `just fuzz`
//! and CI do. Fuzz-instrumented builds (`-ffuzz`, `-Ddiff-coverage`) never use this runner. A
//! fuzz test still runs its corpus and the empty input, as in any unfuzzed build.
//!
//! Not the protocol-less fallback. On the stage2_aarch64, stage2_loongarch, stage2_powerpc
//! and stage2_riscv64 backends the default runner skips the build runner's protocol and runs
//! every test in one loop (`mainSimple`). A shard reports its own tests through that protocol,
//! so this runner does not compile there; build with `-Dtest-shards=false` on those backends.

const builtin = @import("builtin");
const std = @import("std");
const Io = std.Io;
const testing = std.testing;
const assert = std.debug.assert;
const panic = std.debug.panic;

pub const std_options: std.Options = .{
    .logFn = log,
};

comptime {
    switch (builtin.zig_backend) {
        .stage2_aarch64, .stage2_loongarch, .stage2_powerpc, .stage2_riscv64 => @compileError(
            "the shard test runner needs the build runner's protocol, which the default " ++
                "runner skips on this backend: build with -Dtest-shards=false",
        ),
        else => {},
    }
    if (builtin.fuzz) @compileError("the shard test runner does not fuzz: -Dtest-shards=false");
}

var log_err_count: usize = 0;
var fba: std.heap.FixedBufferAllocator = .init(&fba_buffer);
var fba_buffer: [8192]u8 = undefined;
var stdin_buffer: [4096]u8 = undefined;
var stdout_buffer: [4096]u8 = undefined;
var stdin_reader: Io.File.Reader = undefined;
var stdout_writer: Io.File.Writer = undefined;
/// The runner's `Io`, and through `std_options_debug_io` the `Io` of `std.debug` (print,
/// panic messages, error return traces). See `lean_io`.
const runner_io: Io = if (lean_io) StderrIo.io else Io.Threaded.global_single_threaded.io();

/// A binary stripped of debug info (every Debug test binary unless -Dtest-debug-info=true;
/// see build.zig `test_root_module`) gives its runner and `std.debug` the small `StderrIo`
/// instead of `std.Io.Threaded`. Naming `Threaded.io()` pulls Threaded's whole vtable (file
/// systems, processes, DNS, sockets) into the binary: about 80k of an empty test binary's
/// 96k lines of LLVM IR, a third of the compile of a ReleaseSafe test binary that tests KZG
/// (batcher/txmgr). A binary whose code names Threaded (`std.testing.io`, say) still compiles
/// it; worker pools use `utilities/futex` instead. A binary with debug info keeps Threaded,
/// which opens the executable to print file:line and stack traces; `StderrIo` cannot.
const lean_io = builtin.strip_debug_info and builtin.os.tag != .windows;

pub const std_options_debug_io: Io = runner_io;
pub const std_options_debug_threaded_io: ?*Io.Threaded =
    if (lean_io) null else Io.Threaded.global_single_threaded;

/// An `Io` that reads and writes open files (the build runner's stdin and stdout, stderr) and
/// locks stderr for `std.debug`; everything else is `std.Io.failing`. It never cancels, so
/// cancel protection is a no-op, and it does no terminal detection: stderr has no color.
const StderrIo = struct {
    const io: Io = .{ .userdata = null, .vtable = &vtable };

    const vtable: Io.VTable = vtable: {
        var table = Io.failing.vtable.*;
        table.swapCancelProtection = swap_cancel_protection;
        table.checkCancel = check_cancel;
        table.operate = operate;
        table.lockStderr = lock_stderr;
        table.tryLockStderr = try_lock_stderr;
        table.unlockStderr = unlock_stderr;
        break :vtable table;
    };

    /// The id of the thread that holds stderr, plus one; 0 when no thread holds it.
    var stderr_owner: std.atomic.Value(u64) = .init(0);
    /// How many times the owner locked stderr (`std.debug` locks it recursively).
    var stderr_lock_count: u32 = 0;
    var stderr_writer: Io.File.Writer = .{
        .io = io,
        .interface = Io.File.Writer.initInterface(&.{}),
        .file = .stderr(),
        .mode = .streaming,
    };

    fn swap_cancel_protection(
        userdata: ?*anyopaque,
        new: Io.CancelProtection,
    ) Io.CancelProtection {
        assert(userdata == null);
        _ = new;
        return .unblocked;
    }

    fn check_cancel(userdata: ?*anyopaque) Io.Cancelable!void {
        assert(userdata == null);
    }

    fn operate(userdata: ?*anyopaque, operation: Io.Operation) Io.Cancelable!Io.Operation.Result {
        assert(userdata == null);
        return switch (operation) {
            .file_read_streaming => |o| .{ .file_read_streaming = read(o.file, o.data) },
            .file_write_streaming => |o| .{
                .file_write_streaming = write(o.file, o.header, o.data, o.splat),
            },
            else => Io.failingOperate(userdata, operation),
        };
    }

    /// Reads into the first non-empty buffer of `data`.
    fn read(file: Io.File, data: []const []u8) Io.Operation.FileReadStreaming.Error!usize {
        const buffer = for (data) |buffer| {
            if (buffer.len > 0) break buffer;
        } else return 0;
        assert(buffer.len > 0);
        while (true) {
            const rc = std.posix.system.read(file.handle, buffer.ptr, buffer.len);
            switch (std.posix.errno(rc)) {
                .SUCCESS => {
                    const count: usize = @intCast(rc);
                    assert(count <= buffer.len);
                    if (count == 0) return error.EndOfStream;
                    return count;
                },
                .INTR => continue,
                .AGAIN => return error.WouldBlock,
                .BADF => return error.NotOpenForReading,
                .IO => return error.InputOutput,
                .ISDIR => return error.IsDir,
                .CONNRESET => return error.ConnectionResetByPeer,
                else => |err| return std.posix.unexpectedErrno(err),
            }
        }
    }

    /// Writes the first non-empty piece of `header`, `data` and `splat` times the last of
    /// `data`, so it returns how many bytes of those it consumed, as `Io.Writer.drain` does.
    fn write(
        file: Io.File,
        header: []const u8,
        data: []const []const u8,
        splat: usize,
    ) Io.Operation.FileWriteStreaming.Error!usize {
        assert(data.len > 0);
        const pattern = data[data.len - 1];
        const bytes = bytes: {
            if (header.len > 0) break :bytes header;
            for (data[0 .. data.len - 1]) |piece| {
                if (piece.len > 0) break :bytes piece;
            }
            if (splat > 0 and pattern.len > 0) break :bytes pattern;
            return 0;
        };
        assert(bytes.len > 0);
        while (true) {
            const rc = std.posix.system.write(file.handle, bytes.ptr, bytes.len);
            switch (std.posix.errno(rc)) {
                .SUCCESS => {
                    const count: usize = @intCast(rc);
                    assert(count <= bytes.len);
                    return count;
                },
                .INTR => continue,
                .AGAIN => return error.WouldBlock,
                .BADF => return error.NotOpenForWriting,
                .PIPE => return error.BrokenPipe,
                .IO => return error.InputOutput,
                .NOSPC => return error.NoSpaceLeft,
                .DQUOT => return error.DiskQuota,
                .FBIG => return error.FileTooBig,
                else => |err| return std.posix.unexpectedErrno(err),
            }
        }
    }

    fn current_owner() u64 {
        const owner = @as(u64, std.Thread.getCurrentId()) +% 1;
        assert(owner != 0);
        return owner;
    }

    fn lock_stderr(
        userdata: ?*anyopaque,
        terminal_mode: ?Io.Terminal.Mode,
    ) Io.Cancelable!Io.LockedStderr {
        assert(userdata == null);
        const owner = current_owner();
        if (stderr_owner.load(.acquire) != owner) {
            while (stderr_owner.cmpxchgWeak(0, owner, .acquire, .monotonic) != null) {
                std.Thread.yield() catch std.atomic.spinLoopHint();
            }
            assert(stderr_lock_count == 0);
        }
        return locked(terminal_mode);
    }

    fn try_lock_stderr(
        userdata: ?*anyopaque,
        terminal_mode: ?Io.Terminal.Mode,
    ) Io.Cancelable!?Io.LockedStderr {
        assert(userdata == null);
        const owner = current_owner();
        if (stderr_owner.load(.acquire) != owner) {
            if (stderr_owner.cmpxchgStrong(0, owner, .acquire, .monotonic) != null) return null;
            assert(stderr_lock_count == 0);
        }
        return locked(terminal_mode);
    }

    fn locked(terminal_mode: ?Io.Terminal.Mode) Io.LockedStderr {
        assert(stderr_owner.load(.monotonic) == current_owner());
        stderr_lock_count += 1;
        return .{
            .file_writer = &stderr_writer,
            .terminal_mode = terminal_mode orelse .no_color,
        };
    }

    fn unlock_stderr(userdata: ?*anyopaque) void {
        assert(userdata == null);
        assert(stderr_owner.load(.monotonic) == current_owner());
        assert(stderr_lock_count > 0);
        if (stderr_writer.err == null) stderr_writer.interface.flush() catch {};
        stderr_writer.err = null;
        stderr_writer.interface.end = 0;
        stderr_writer.interface.buffer = &.{};
        stderr_lock_count -= 1;
        if (stderr_lock_count == 0) stderr_owner.store(0, .release);
    }
};

/// Indexes into `builtin.test_functions` of this run's tests, in binary order; allocated once
/// at startup.
var shard_tests: []const u32 = &.{};

/// Shard `index` of `count`: the tests whose name hashes to `index` modulo `count`.
pub const Shard = struct {
    index: u32,
    count: u32,

    pub const all: Shard = .{ .index = 0, .count = 1 };

    /// Parses "K/N": two plain decimal numbers (digits only), N >= 1 and K < N.
    pub fn parse(text: []const u8) ?Shard {
        const slash = std.mem.indexOfScalar(u8, text, '/') orelse return null;
        const index = parse_decimal(text[0..slash]) orelse return null;
        const count = parse_decimal(text[slash + 1 ..]) orelse return null;
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

/// A u32 written as 1 to 10 ASCII digits and nothing else: no sign, no `_` separator.
fn parse_decimal(text: []const u8) ?u32 {
    if (text.len == 0 or text.len > 10) return null;
    var value: u64 = 0;
    for (text) |char| {
        if (char < '0' or char > '9') return null;
        value = value * 10 + (char - '0');
    }
    assert(value < 10_000_000_000);
    return std.math.cast(u32, value);
}

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
    const args = init.args.toSlice(fba.allocator()) catch |err|
        panic("unable to parse command line args: {t}", .{err});

    var listen = false;
    var shard_given = false;
    var shard: Shard = .all;
    for (args[1..]) |arg| {
        if (std.mem.eql(u8, arg, "--listen=-")) {
            listen = true;
        } else if (std.mem.startsWith(u8, arg, "--seed=")) {
            testing.random_seed = std.fmt.parseUnsigned(u32, arg["--seed=".len..], 0) catch
                @panic("unable to parse --seed command line argument");
        } else if (std.mem.startsWith(u8, arg, "--cache-dir")) {
            // The build runner always passes it; only fuzzing reads it.
        } else if (std.mem.startsWith(u8, arg, "--shard=")) {
            if (shard_given) panic("--shard given twice: {s}", .{arg});
            shard_given = true;
            shard = Shard.parse(arg["--shard=".len..]) orelse
                panic("invalid argument {s}: expected --shard=K/N with K < N", .{arg});
        } else {
            panic("unrecognized command line argument: {s}", .{arg});
        }
    }
    select(shard);

    if (listen) {
        return main_server(init) catch |err| panic("internal test runner failure: {t}", .{err});
    } else {
        return main_terminal(init);
    }
}

fn main_server(init: std.process.Init.Minimal) !void {
    stdin_reader = .initStreaming(.stdin(), runner_io, &stdin_buffer);
    stdout_writer = .initStreaming(.stdout(), runner_io, &stdout_buffer);
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
            else => {
                // Includes start_fuzzing, which no shard asks for: it reports no fuzz test.
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
            // The build runner shows only what a failed test wrote to stderr. Without this
            // line a test with no error return trace (no debug info) "failed without output".
            std.debug.print("'{s}' failed with error.{t}\n", .{ test_fn.name, err });
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
            // Never a fuzz test: see "Not fuzzing" at the top of this file.
            .fuzz = false,
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

const Counts = struct {
    ok: u32 = 0,
    skip: u32 = 0,
    fail: u32 = 0,
    fuzz: u32 = 0,
    leaks: u32 = 0,
};

fn main_terminal(init: std.process.Init.Minimal) void {
    var counts: Counts = .{};
    // `StderrIo` cannot draw progress or detect a terminal: it prints each test's line.
    const root_node = std.Progress.start(runner_io, .{
        .root_name = "Test",
        .estimated_total_items = shard_tests.len,
        .disable_printing = lean_io,
    });
    const have_tty = !lean_io and (Io.File.stderr().isTty(runner_io) catch false);

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

/// Set when a test calls `std.testing.fuzz`; counted in terminal mode, never reported.
var is_fuzz_test: bool = undefined;

/// `std.testing.fuzz` calls this through `@import("root")`. As in the default runner when the
/// binary is not built for fuzzing: run the corpus, then the empty input.
pub fn fuzz(
    context: anytype,
    comptime testOne: fn (context: @TypeOf(context), *std.testing.Smith) anyerror!void,
    options: testing.FuzzInputOptions,
) anyerror!void {
    comptime assert(!builtin.fuzz);
    is_fuzz_test = true;

    // Ensure no test failure occurred before starting fuzzing.
    if (log_err_count != 0) @panic("error logs detected");

    for (options.corpus) |input| {
        var smith: testing.Smith = .{ .in = input };
        try testOne(context, &smith);
    }

    // In case there is no provided corpus, also use an empty
    // string as a smoke test.
    var smith: testing.Smith = .{ .in = "" };
    try testOne(context, &smith);
}
