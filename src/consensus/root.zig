//! `consensus` group. Mirrors base/base `crates/consensus`. Sub-modules are re-exported
//! here by package name as they land, e.g. `pub const rlp = @import("consensus/rlp");`.

const std = @import("std");

pub const protocol = @import("consensus/protocol");
pub const p2p = @import("consensus/p2p");
pub const derive = @import("consensus/derive");
pub const derive_check = @import("consensus/derive_check");
pub const safedb = @import("consensus/safedb");
pub const engine = @import("consensus/engine");
pub const rpc = @import("consensus/rpc");
pub const sequencer = @import("consensus/sequencer");
pub const sources = @import("consensus/sources");
pub const sources_fixtures = @import("consensus/sources_fixtures");
pub const service = @import("consensus/service");

test {
    std.testing.refAllDecls(@This());
}
