//! Futex wait and wake on a `u32` word, straight to the OS: Linux `futex(2)` and Darwin
//! `__ulock_wait`/`__ulock_wake`, the calls `std.Io.Threaded` makes. A futex is a
//! synchronization primitive, not I/O, so the worker pools that use one (the trie's rehash
//! pool, the node's sender recovery) need no `Io`.
//!
//! Calling them through `std.Io.Threaded.global_single_threaded.io()` instead names Threaded's
//! whole vtable (file systems, processes, DNS, sockets), which every binary that links a pool
//! then compiles: tens of thousands of lines of LLVM IR in each test binary above the trie.
//! Other targets keep that path.

const std = @import("std");
const builtin = @import("builtin");
const assert = std.debug.assert;

const os = builtin.os.tag;
const direct = os == .linux or os.isDarwin();

/// Blocks while `word` holds `expected`, until a `wake`. It may return spuriously: the caller
/// rechecks its condition and waits again, as with any futex.
pub fn wait(word: *const std.atomic.Value(u32), expected: u32) void {
    comptime assert(!builtin.single_threaded); // Nobody could wake the caller.
    if (!direct) return fallback().futexWaitUncancelable(u32, &word.raw, expected);
    if (os == .linux) {
        const linux = std.os.linux;
        const rc = linux.futex_4arg(&word.raw, .{ .cmd = .WAIT, .private = true }, expected, null);
        switch (linux.errno(rc)) {
            // Woken, interrupted, or `word` no longer held `expected`: the caller rechecks.
            .SUCCESS, .INTR, .AGAIN => {},
            else => |err| std.debug.panic("futex wait: unexpected errno {t}", .{err}),
        }
    } else {
        const flags: std.c.UL = .{ .op = .COMPARE_AND_WAIT, .NO_ERRNO = true };
        const status = std.c.__ulock_wait(flags, &word.raw, expected, 0);
        if (status >= 0) return;
        switch (@as(std.c.E, @fromBackingInt(@intCast(-status)))) {
            // Interrupted, or the page holding `word` was out: the caller rechecks.
            .INTR, .FAULT => {},
            else => |err| std.debug.panic("futex wait: unexpected errno {t}", .{err}),
        }
    }
}

/// Wakes up to `waiters_max` threads blocked in `wait` on `word`.
pub fn wake(word: *const std.atomic.Value(u32), waiters_max: u32) void {
    assert(waiters_max > 0);
    if (builtin.single_threaded) return; // Nobody waits.
    if (!direct) return fallback().futexWake(u32, &word.raw, waiters_max);
    if (os == .linux) {
        const linux = std.os.linux;
        const count = @min(waiters_max, std.math.maxInt(i32));
        const rc = linux.futex_3arg(&word.raw, .{ .cmd = .WAKE, .private = true }, count);
        switch (linux.errno(rc)) {
            .SUCCESS => {},
            else => |err| std.debug.panic("futex wake: unexpected errno {t}", .{err}),
        }
    } else {
        const flags: std.c.UL = .{
            .op = .COMPARE_AND_WAIT,
            .NO_ERRNO = true,
            .WAKE_ALL = waiters_max > 1,
        };
        while (true) {
            const status = std.c.__ulock_wake(flags, &word.raw, 0);
            if (status >= 0) return;
            switch (@as(std.c.E, @fromBackingInt(@intCast(-status)))) {
                .INTR, .CANCELED => continue, // Interrupted before waking anyone: retry.
                .NOENT => return, // Nobody was waiting.
                else => |err| std.debug.panic("futex wake: unexpected errno {t}", .{err}),
            }
        }
    }
}

fn fallback() std.Io {
    comptime assert(!direct);
    return std.Io.Threaded.global_single_threaded.io();
}

test "futex: wake releases a waiter" {
    if (builtin.single_threaded) return error.SkipZigTest;
    const Shared = struct {
        word: std.atomic.Value(u32) = .init(0),

        fn waiter(shared: *@This()) void {
            while (shared.word.load(.acquire) == 0) wait(&shared.word, 0);
            assert(shared.word.load(.acquire) == 1);
        }
    };
    var shared: Shared = .{};
    const thread = try std.Thread.spawn(.{}, Shared.waiter, .{&shared});
    shared.word.store(1, .release);
    wake(&shared.word, 1);
    thread.join();
    try std.testing.expectEqual(@as(u32, 1), shared.word.load(.acquire));
}

test "futex: wait returns at once when the word changed, and waking nobody is fine" {
    if (builtin.single_threaded) return error.SkipZigTest;
    var word: std.atomic.Value(u32) = .init(7);
    wait(&word, 3); // `word` holds 7, not 3: no block.
    wake(&word, 1);
    wake(&word, std.math.maxInt(u32));
    try std.testing.expectEqual(@as(u32, 7), word.load(.acquire));
}
