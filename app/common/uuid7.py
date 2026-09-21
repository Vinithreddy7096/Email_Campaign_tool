"""
UUIDv7 (RFC 9562 §5.7) — time-sortable primary keys.

BUILD.md §6 requires UUIDv7 on every table. ``uuid.uuid7`` only lands in the
CPython stdlib in 3.14, so this is the implementation until then.

Layout::

    0                   1                   2                   3
    |unix_ts_ms (48 bits)                    |ver|rand_a |var|rand_b (62)|

Ordering is by millisecond. Two IDs minted inside the same millisecond have no
guaranteed relative order — that is acceptable for index locality, and code must
not rely on intra-millisecond ordering for correctness.
"""

from __future__ import annotations

import secrets
import time
import uuid

__all__ = ["uuid7", "timestamp_ms"]


def uuid7() -> uuid.UUID:
    """Return a new time-sortable UUIDv7."""
    ts_ms = time.time_ns() // 1_000_000
    data = bytearray(ts_ms.to_bytes(6, "big") + secrets.token_bytes(10))
    data[6] = (data[6] & 0x0F) | 0x70  # version 7
    data[8] = (data[8] & 0x3F) | 0x80  # variant 0b10
    return uuid.UUID(bytes=bytes(data))


def timestamp_ms(value: uuid.UUID) -> int:
    """Extract the embedded millisecond timestamp from a UUIDv7."""
    if value.version != 7:
        raise ValueError(f"not a UUIDv7: {value} (version {value.version})")
    return int.from_bytes(value.bytes[:6], "big")
