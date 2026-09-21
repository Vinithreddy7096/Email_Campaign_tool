"""UUIDv7 (RFC 9562 §5.7) — time-sortable primary keys, BUILD.md §6."""

import time
import uuid

import pytest

from common.uuid7 import timestamp_ms, uuid7


def test_version_and_variant_bits():
    value = uuid7()
    assert value.version == 7
    # RFC 9562 variant is 0b10xxxxxx in octet 8.
    assert value.bytes[8] >> 6 == 0b10


def test_values_are_unique():
    assert len({uuid7() for _ in range(10_000)}) == 10_000


def test_embedded_timestamp_matches_wall_clock():
    before = time.time_ns() // 1_000_000
    value = uuid7()
    after = time.time_ns() // 1_000_000
    assert before <= timestamp_ms(value) <= after


def test_sorts_by_time_across_milliseconds():
    first = uuid7()
    time.sleep(0.005)
    second = uuid7()
    # Lexicographic order of the string form must follow creation order, which
    # is the property that gives index locality on a UUID primary key.
    assert str(first) < str(second)
    assert first.bytes < second.bytes


def test_timestamp_ms_rejects_other_uuid_versions():
    with pytest.raises(ValueError, match="not a UUIDv7"):
        timestamp_ms(uuid.uuid4())
