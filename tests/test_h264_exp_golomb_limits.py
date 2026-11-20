"""Exp-Golomb slice headers must stop once the leading-zero run exceeds 32 bits.

A 16-byte head is parsed first. When that head does not finish the value, the
same decoder runs again over the whole NAL. Either walk shifts a Python integer
once per suffix bit, so a wide suffix stalls the caller. These tests fail while
that walk is unbounded and pass when a leading-zero run longer than 32 bits is
rejected with the existing truncation error before the suffix is materialized.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest
from hebbian_mcap.h264 import (
    count_h264_pictures,
    ensure_access_unit_delimiter,
    scan_picture_coding_types,
)
from hebbian_mcap.video import McapMediaError, export_mcap_camera
from mcap_test_helpers import write_compressed_video_mcap

# One more than the widest leading-zero run a repaired decoder may accept.
_OVERLONG_LEADING_ZEROS = 33
# Half a million suffix bits are about 128 KiB and take seconds to shift in.
_UNBOUNDED_LEADING_ZEROS = 524_288
_REJECTION_DEADLINE_SECONDS = 0.75
_TRUNCATED_VALUE = "slice header truncates its first_mb_in_slice value"
_CAMERA_TOPIC = "/camera/front"
_FIRST_LOG_TIME_NS = 1_700_000_000_000_000_000
_SECOND_LOG_TIME_NS = _FIRST_LOG_TIME_NS + 20_000_000


def _slice_annex_b(leading_zero_bits: int, nal_header: int = 0x65) -> bytes:
    """One Annex B slice whose first field has ``leading_zero_bits`` zeros.

    The stop bit and every suffix bit are set, and ``slice_type`` is P (code
    0), so a decoder that accepts the width finishes both fields. The first 16
    payload bytes are zeros when the run is longer than that head, which is
    what forces the full-payload fallback.
    """
    bits = "0" * leading_zero_bits + "1" + "1" * leading_zero_bits + "1"
    bits += "0" * ((8 - len(bits) % 8) % 8)
    payload = bytes(int(bits[index : index + 8], 2) for index in range(0, len(bits), 8))
    return b"\x00\x00\x00\x01" + bytes((nal_header,)) + payload


def _assert_value_error_quickly(operation: Callable[[], object], match: str) -> None:
    started = time.perf_counter()
    with pytest.raises(ValueError, match=match):
        operation()
    elapsed = time.perf_counter() - started
    assert elapsed < _REJECTION_DEADLINE_SECONDS


def test_head_parse_rejects_an_exp_golomb_wider_than_32_bits() -> None:
    """33 leading zeros plus their suffix still fit in the 16-byte head."""
    stream = _slice_annex_b(_OVERLONG_LEADING_ZEROS)
    assert len(stream) - 5 <= 16

    _assert_value_error_quickly(lambda: count_h264_pictures(stream), _TRUNCATED_VALUE)
    _assert_value_error_quickly(
        lambda: scan_picture_coding_types(stream), "incomplete or truncated"
    )
    _assert_value_error_quickly(lambda: ensure_access_unit_delimiter(stream), _TRUNCATED_VALUE)


@pytest.mark.parametrize("nal_header", [0x41, 0x42, 0x65])
def test_every_slice_header_nal_type_rejects_an_overlong_exp_golomb(nal_header: int) -> None:
    stream = _slice_annex_b(_OVERLONG_LEADING_ZEROS, nal_header)
    _assert_value_error_quickly(lambda: count_h264_pictures(stream), _TRUNCATED_VALUE)


def test_picture_count_rejects_an_unbounded_suffix_before_building_it() -> None:
    stream = _slice_annex_b(_UNBOUNDED_LEADING_ZEROS)
    assert stream[5:21] == b"\x00" * 16

    _assert_value_error_quickly(lambda: count_h264_pictures(stream), _TRUNCATED_VALUE)


def test_coding_type_scan_rejects_an_unbounded_suffix_before_building_it() -> None:
    stream = _slice_annex_b(_UNBOUNDED_LEADING_ZEROS)
    _assert_value_error_quickly(
        lambda: scan_picture_coding_types(stream), "incomplete or truncated"
    )


def test_access_unit_repair_rejects_an_unbounded_suffix_before_building_it() -> None:
    stream = _slice_annex_b(_UNBOUNDED_LEADING_ZEROS)
    _assert_value_error_quickly(lambda: ensure_access_unit_delimiter(stream), _TRUNCATED_VALUE)


def test_camera_export_rejects_an_unbounded_suffix_without_publishing(tmp_path: Path) -> None:
    payload = _slice_annex_b(_UNBOUNDED_LEADING_ZEROS)
    source = tmp_path / "camera.mcap"
    output = tmp_path / "camera.mp4"
    write_compressed_video_mcap(
        source,
        [
            (_CAMERA_TOPIC, _FIRST_LOG_TIME_NS, payload),
            (_CAMERA_TOPIC, _SECOND_LOG_TIME_NS, payload),
        ],
    )

    started = time.perf_counter()
    with pytest.raises(McapMediaError, match=_TRUNCATED_VALUE):
        export_mcap_camera(source, output)
    elapsed = time.perf_counter() - started

    assert elapsed < _REJECTION_DEADLINE_SECONDS
    assert not output.exists()
    assert not list(tmp_path.glob("*.mp4"))
