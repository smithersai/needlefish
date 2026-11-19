//! Coverage for coverage-guided fuzzing (`BASE_DIFF_COVERAGE`, needs a `-Ddiff-coverage`
//! build): each response gets `"coverage": {"nf:<feature>": count, ...}`, which the harness
//! does not compare. Features are needlefish's semantic features (`evm.coverage`) and its
//! edges, `nf:edge:<index>`: the compiler's 8-bit counters (`-ffuzz`) whose code lies in an
//! EVM or crypto function (`edge_filter`), hit counts modulo 256, reset per request. Edge
//! indices are stable for one binary only.

const std = @import("std");
const assert = std.debug.assert;
const elf = std.elf;

const evm = @import("execution/evm");

/// Functions whose edges are reported: the EVM (interpreter, precompiles, native
/// precompiles, transaction processing) and the crypto behind the precompiles. Zig names a
/// function by its file's path in the `base` module.
pub const function_prefixes = [_][]const u8{ "execution.evm.", "common.crypto." };

pub const Coverage = struct {
    /// Indices into the edge counters, ascending.
    edges: []const u32,
    counters: []u8,

    /// `elf_bytes` is this executable (for its symbol table). Without the instrumentation,
    /// only semantic features are reported.
    pub fn init(allocator: std.mem.Allocator, elf_bytes: []const u8) !Coverage {
        assert(evm.coverage.enabled);
        const counters = evm.coverage.edge_counters();
        const pcs = evm.coverage.edge_pcs();
        assert(counters.len == pcs.len);
        const edges = try edge_filter(allocator, elf_bytes, pcs);
        evm.coverage.active = true;
        return .{ .edges = edges, .counters = counters };
    }

    pub fn deinit(coverage: *Coverage, allocator: std.mem.Allocator) void {
        allocator.free(coverage.edges);
        evm.coverage.active = false;
        coverage.* = undefined;
    }

    /// Clears every feature before a request.
    pub fn begin(coverage: *const Coverage) void {
        assert(evm.coverage.active);
        evm.coverage.reset();
        @memset(coverage.counters, 0);
    }

    /// `,"coverage":{...}`
    pub fn write(coverage: *const Coverage, out: *std.Io.Writer) std.Io.Writer.Error!void {
        try out.writeAll(",\"coverage\":{");
        var first = try evm.coverage.write_members(out, true);
        for (coverage.edges) |index| {
            const count = coverage.counters[index];
            if (count == 0) continue;
            try evm.coverage.separator(out, &first);
            try out.print("\"nf:edge:{d}\":{d}", .{ index, count });
        }
        try out.writeAll("}");
    }
};

const Range = struct { start: u64, end: u64 };

/// Indices of the `pcs` (one per edge counter) inside a function named with one of
/// `function_prefixes`, read from the ELF symbol table in `elf_bytes`.
pub fn edge_filter(
    allocator: std.mem.Allocator,
    elf_bytes: []const u8,
    pcs: []const usize,
) ![]u32 {
    var ranges: std.ArrayList(Range) = .empty;
    defer ranges.deinit(allocator);
    try function_ranges(allocator, elf_bytes, &ranges);
    std.mem.sort(Range, ranges.items, {}, range_less);
    var edges: std.ArrayList(u32) = .empty;
    errdefer edges.deinit(allocator);
    for (pcs, 0..) |pc, index| {
        if (contains(ranges.items, pc)) try edges.append(allocator, @intCast(index));
    }
    assert(edges.items.len <= pcs.len);
    return edges.toOwnedSlice(allocator);
}

fn range_less(_: void, a: Range, b: Range) bool {
    return a.start < b.start;
}

/// Ranges may overlap (aliases); a PC in any of them counts.
fn contains(ranges: []const Range, pc: u64) bool {
    // The last range starting at or before `pc`, then a short scan back for aliases.
    var low: usize = 0;
    var high: usize = ranges.len;
    while (low < high) {
        const middle = low + (high - low) / 2;
        if (ranges[middle].start <= pc) low = middle + 1 else high = middle;
    }
    var index = low;
    var scanned: u32 = 0;
    while (index > 0 and scanned < 16) : (scanned += 1) {
        index -= 1;
        if (pc < ranges[index].end) return true;
    }
    return false;
}

const Error = error{InvalidElf};

fn function_ranges(
    allocator: std.mem.Allocator,
    bytes: []const u8,
    ranges: *std.ArrayList(Range),
) !void {
    if (bytes.len < @sizeOf(elf.Elf64_Ehdr)) return error.InvalidElf;
    if (!std.mem.eql(u8, bytes[0..4], elf.MAGIC)) return error.InvalidElf;
    if (bytes[elf.EI.CLASS] != elf.ELFCLASS64) return error.InvalidElf;
    const header = std.mem.bytesToValue(elf.Elf64_Ehdr, bytes[0..@sizeOf(elf.Elf64_Ehdr)]);
    const sections = try section_headers(bytes, &header);
    for (sections) |section| {
        if (section.sh_type != elf.SHT_SYMTAB) continue;
        if (section.sh_link >= sections.len) return error.InvalidElf;
        const strings = try section_bytes(bytes, &sections[section.sh_link]);
        const symbols = try section_bytes(bytes, &section);
        const count = symbols.len / @sizeOf(elf.Elf64_Sym);
        for (0..count) |index| {
            const offset = index * @sizeOf(elf.Elf64_Sym);
            const symbol = std.mem.bytesToValue(
                elf.Elf64_Sym,
                symbols[offset..][0..@sizeOf(elf.Elf64_Sym)],
            );
            if (symbol.st_type() != elf.STT_FUNC or symbol.st_size == 0) continue;
            if (symbol.st_name >= strings.len) return error.InvalidElf;
            const name = std.mem.sliceTo(strings[symbol.st_name..], 0);
            if (!selected(name)) continue;
            const end = std.math.add(u64, symbol.st_value, symbol.st_size) catch
                return error.InvalidElf;
            try ranges.append(allocator, .{ .start = symbol.st_value, .end = end });
        }
    }
}

fn selected(name: []const u8) bool {
    for (function_prefixes) |prefix| {
        if (std.mem.startsWith(u8, name, prefix)) return true;
    }
    return false;
}

const Sections = []align(1) const elf.Elf64_Shdr;

fn section_headers(bytes: []const u8, header: *const elf.Elf64_Ehdr) Error!Sections {
    if (header.e_shentsize != @sizeOf(elf.Elf64_Shdr)) return error.InvalidElf;
    const size = @as(u64, header.e_shnum) * @sizeOf(elf.Elf64_Shdr);
    if (header.e_shoff > bytes.len or size > bytes.len - header.e_shoff) return error.InvalidElf;
    const start: usize = @intCast(header.e_shoff);
    return std.mem.bytesAsSlice(elf.Elf64_Shdr, bytes[start..][0..@intCast(size)]);
}

fn section_bytes(bytes: []const u8, section: *align(1) const elf.Elf64_Shdr) Error![]const u8 {
    if (section.sh_offset > bytes.len or section.sh_size > bytes.len - section.sh_offset) {
        return error.InvalidElf;
    }
    const start: usize = @intCast(section.sh_offset);
    return bytes[start..][0..@intCast(section.sh_size)];
}

test "diff coverage: edge filter keeps PCs inside selected functions" {
    const ranges = [_]Range{
        .{ .start = 0x100, .end = 0x180 },
        .{ .start = 0x200, .end = 0x210 },
    };
    try std.testing.expect(contains(&ranges, 0x100));
    try std.testing.expect(contains(&ranges, 0x17f));
    try std.testing.expect(!contains(&ranges, 0x180));
    try std.testing.expect(!contains(&ranges, 0xff));
    try std.testing.expect(contains(&ranges, 0x20f));
    try std.testing.expect(!contains(&ranges, 0x1000));
    try std.testing.expect(selected("execution.evm.interpreter.run__anon_1"));
    try std.testing.expect(!selected("execution.node.executor.Executor.apply"));
}
