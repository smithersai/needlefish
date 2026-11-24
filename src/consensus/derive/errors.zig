//! Derivation pipeline errors. base/base wraps every error in a kind: temporary (retry
//! later, usually waiting for L1 data), reset (rewind the pipeline to a safe L1 origin),
//! or critical (the pipeline cannot continue). Zig errors carry no payload, so the kind
//! is a pure function of the error value.
//! Mirrors base/base `crates/consensus/derive/src/errors/pipeline.rs`.

const std = @import("std");
const assert = std.debug.assert;

pub const Kind = enum { temporary, reset, critical };

/// Errors returned by chain and blob providers.
pub const ProviderError = error{
    /// The data is not available yet (fetch in flight, or not yet produced). Retry.
    NotAvailable,
    /// The block or receipt is unknown to the provider. Treated as temporary, like a
    /// provider that is behind the chain head; derivation's L1 lookups by hash turn it into
    /// `BlockNotFound` (`hash_lookup`).
    NotFound,
    /// The beacon node permanently lacks the blobs (missed or orphaned slot).
    BlobsUnavailable,
    /// The execution client pruned the block body (no L1 info deposit to decode); only the
    /// header is known. Derivation cannot go below it; startup recovers from checkpoints.
    Pruned,
    OutOfMemory,
};

pub const Error = ProviderError || error{
    // Temporary.
    Eof,
    NotEnoughData,
    MissingL1Data,
    ChannelReaderEmpty,
    /// The pipeline's prepared attributes queue is full; the driver must drain it.
    PreparedAttributesFull,
    // Reset.
    ReorgDetected,
    HoloceneActivation,
    BadParentHash,
    BadTimestamp,
    L1OriginMismatch,
    /// The new epoch's L1 block does not build on the parent's L1 origin.
    EpochParentMismatch,
    NextL1BlockHashMismatch,
    BlobsUnderFill,
    BlobsOverFill,
    /// An L1 block the pipeline asked for by hash is unknown: an L1 reorg removed it.
    BlockNotFound,
    // Critical.
    MissingOrigin,
    ChannelProviderEmpty,
    ChannelNotFound,
    InvalidBatchType,
    InvalidBatchValidity,
    BatchQueueEmpty,
    AttributesBuilder,
    /// Malformed L1 data, e.g. an undecodable deposit log.
    BadEncoding,
};

pub fn kind(err: Error) Kind {
    return switch (err) {
        error.NotAvailable,
        error.NotFound,
        error.Eof,
        error.NotEnoughData,
        error.MissingL1Data,
        error.ChannelReaderEmpty,
        error.PreparedAttributesFull,
        => .temporary,
        error.ReorgDetected,
        error.HoloceneActivation,
        error.BadParentHash,
        error.BadTimestamp,
        error.L1OriginMismatch,
        error.EpochParentMismatch,
        error.NextL1BlockHashMismatch,
        error.BlobsUnavailable,
        error.BlobsUnderFill,
        error.BlobsOverFill,
        error.BlockNotFound,
        => .reset,
        error.OutOfMemory,
        error.Pruned,
        error.MissingOrigin,
        error.ChannelProviderEmpty,
        error.ChannelNotFound,
        error.InvalidBatchType,
        error.InvalidBatchValidity,
        error.BatchQueueEmpty,
        error.AttributesBuilder,
        error.BadEncoding,
        => .critical,
    };
}

/// The error of an L1 lookup by hash. A block the pipeline names by hash came from an
/// earlier lookup, so the provider not knowing it means an L1 reorg removed it, and asking
/// again cannot succeed: the pipeline resets. A lookup by number that misses stays
/// temporary (the block is not produced yet). Mirrors base/base
/// `crates/consensus/providers/src/chain_provider.rs` (`BlockNotFound` of a hash is a
/// reset, of a number temporary).
pub fn hash_lookup(err: ProviderError) Error {
    return if (err == error.NotFound) error.BlockNotFound else err;
}

test "errors: kinds" {
    try std.testing.expectEqual(Kind.temporary, kind(error.Eof));
    try std.testing.expectEqual(Kind.temporary, kind(error.NotAvailable));
    try std.testing.expectEqual(Kind.reset, kind(error.BlobsUnavailable));
    try std.testing.expectEqual(Kind.reset, kind(error.HoloceneActivation));
    try std.testing.expectEqual(Kind.critical, kind(error.OutOfMemory));
    assert(kind(error.MissingOrigin) == .critical);
}

test "errors: an L1 lookup by hash that misses resets, one in flight retries" {
    try std.testing.expectEqual(error.BlockNotFound, hash_lookup(error.NotFound));
    try std.testing.expectEqual(Kind.reset, kind(hash_lookup(error.NotFound)));
    try std.testing.expectEqual(error.NotAvailable, hash_lookup(error.NotAvailable));
    try std.testing.expectEqual(Kind.temporary, kind(hash_lookup(error.NotAvailable)));
    try std.testing.expectEqual(Kind.temporary, kind(error.NotFound));
}
