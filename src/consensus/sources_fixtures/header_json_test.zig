//! `header_json` against a block that execution/rpc's formatter wrote (fixtures.zig).

const std = @import("std");

const fixtures = @import("fixtures.zig");
const header_json = @import("consensus/sources").header_json;

const testing = std.testing;
const decode = header_json.decode;
const extra_data_size_max = header_json.extra_data_size_max;
const transactions_raw = header_json.transactions_raw;

test "header json: decodes and verifies the hash" {
    const header = fixtures.header_for(&.{}, 42, .init(@splat(3)));
    var buffer: [4096]u8 = undefined;
    const text = try fixtures.block_json(&buffer, &header, &.{}, false);
    var extra: [extra_data_size_max]u8 = undefined;
    const decoded = try decode(text, &extra);
    try testing.expect(decoded.hash.eql(header.hash()));
    try testing.expectEqualStrings("needlefish", decoded.header.extra_data);
    try testing.expectEqual(@as(u64, 42), decoded.block_info().number);
    try testing.expectEqual(@as(?u64, 0), decoded.l1_header().excess_blob_gas);
    try testing.expectEqualStrings("[]", (try transactions_raw(text)).?);
    const at = std.mem.indexOf(u8, text, "\"timestamp\":\"0x").? + 15;
    buffer[at] = if (buffer[at] == '1') '2' else '1';
    try testing.expectError(error.HashMismatch, decode(text, &extra));
}
