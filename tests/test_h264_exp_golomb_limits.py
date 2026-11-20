"""Exp-Golomb slice headers must stop once the leading-zero run exceeds 32 bits.

A 16-byte head is parsed first, and the whole NAL is unescaped and parsed again
only when that head ends before the header does. A value wider than the limit
is rejected with its own error before its suffix is read, and that rejection
is final: no longer prefix can make the value narrower, so it never triggers
the full-payload retry.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest
from hebbian_mcap import h264
from hebbian_mcap.h264 import (
    count_h264_pictures,
    ensure_access_unit_delimiter,
    scan_picture_coding_types,
)
from hebbian_mcap.video import McapMediaError, export_mcap_camera
from mcap_test_helpers import write_compressed_video_mcap

_WIDEST_ACCEPTED_LEADING_ZEROS = 32
_OVERLONG_LEADING_ZEROS = _WIDEST_ACCEPTED_LEADING_ZEROS + 1
# Half a million suffix bits are about 128 KiB and take seconds to shift in.
_UNBOUNDED_LEADING_ZEROS = 524_288
_SLICE_HEADER_HEAD_BYTES = 16
_REJECTION_DEADLINE_SECONDS = 0.75
_TOO_WIDE_VALUE = "slice header has an Exp-Golomb value with more than 32 leading zero bits"
_CAMERA_TOPIC = "/camera/front"
_FIRST_LOG_TIME_NS = 1_700_000_000_000_000_000
_SECOND_LOG_TIME_NS = _FIRST_LOG_TIME_NS + 20_000_000


def _pack_bits(bits: str) -> bytes:
    bits += "0" * (-len(bits) % 8)
    return bytes(int(bits[index : index + 8], 2) for index in range(0, len(bits), 8))


def _slice_annex_b(leading_zero_bits: int, nal_header: int = 0x65) -> bytes:
    """One Annex B slice whose first field has ``leading_zero_bits`` zeros.

    The stop bit and every suffix bit are set, and ``slice_type`` is P (code
    0), so a decoder that accepts the width finishes both fields. The first 16
    payload bytes are zeros when the run is longer than that head.
    """
    bits = "0" * leading_zero_bits + "1" + "1" * leading_zero_bits + "1"
    return b"\x00\x00\x00\x01" + bytes((nal_header,)) + _pack_bits(bits)


def _escape_emulation(rbsp: bytes) -> bytes:
    """Insert 0x03 after each 00 00 pair followed by a byte at or below 0x03."""
    encoded = bytearray()
    consecutive_zeros = 0
    for byte in rbsp:
        if consecutive_zeros >= 2 and byte <= 0x03:
            encoded.append(0x03)
            consecutive_zeros = 0
        encoded.append(byte)
        consecutive_zeros = consecutive_zeros + 1 if byte == 0 else 0
    return bytes(encoded)


def _assert_value_error_quickly(operation: Callable[[], object], match: str) -> None:
    started = time.perf_counter()
    with pytest.raises(ValueError, match=match):
        operation()
    elapsed = time.perf_counter() - started
    assert elapsed < _REJECTION_DEADLINE_SECONDS


def test_head_parse_rejects_an_exp_golomb_wider_than_32_bits() -> None:
    """33 leading zeros plus their suffix still fit in the 16-byte head."""
    stream = _slice_annex_b(_OVERLONG_LEADING_ZEROS)
    assert len(stream) - 5 <= _SLICE_HEADER_HEAD_BYTES

    _assert_value_error_quickly(lambda: count_h264_pictures(stream), _TOO_WIDE_VALUE)
    _assert_value_error_quickly(lambda: scan_picture_coding_types(stream), _TOO_WIDE_VALUE)
    _assert_value_error_quickly(lambda: ensure_access_unit_delimiter(stream), _TOO_WIDE_VALUE)


def test_head_parse_accepts_the_widest_permitted_exp_golomb() -> None:
    """32 leading zeros decode; first_mb_in_slice is then nonzero, so no picture starts."""
    stream = _slice_annex_b(_WIDEST_ACCEPTED_LEADING_ZEROS)

    assert count_h264_pictures(stream) == 0
    with pytest.raises(ValueError, match="before any picture starts"):
        scan_picture_coding_types(stream)


@pytest.mark.parametrize("nal_header", [0x41, 0x42, 0x65])
def test_every_slice_header_nal_type_rejects_an_overlong_exp_golomb(nal_header: int) -> None:
    stream = _slice_annex_b(_OVERLONG_LEADING_ZEROS, nal_header)
    _assert_value_error_quickly(lambda: count_h264_pictures(stream), _TOO_WIDE_VALUE)


def test_picture_count_rejects_an_unbounded_suffix_before_building_it() -> None:
    stream = _slice_annex_b(_UNBOUNDED_LEADING_ZEROS)
    assert stream[5:21] == b"\x00" * 16

    _assert_value_error_quickly(lambda: count_h264_pictures(stream), _TOO_WIDE_VALUE)


def test_coding_type_scan_rejects_an_unbounded_suffix_before_building_it() -> None:
    stream = _slice_annex_b(_UNBOUNDED_LEADING_ZEROS)
    _assert_value_error_quickly(lambda: scan_picture_coding_types(stream), _TOO_WIDE_VALUE)


def test_access_unit_repair_rejects_an_unbounded_suffix_before_building_it() -> None:
    stream = _slice_annex_b(_UNBOUNDED_LEADING_ZEROS)
    _assert_value_error_quickly(lambda: ensure_access_unit_delimiter(stream), _TOO_WIDE_VALUE)


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
    with pytest.raises(McapMediaError, match=_TOO_WIDE_VALUE):
        export_mcap_camera(source, output)
    elapsed = time.perf_counter() - started

    assert elapsed < _REJECTION_DEADLINE_SECONDS
    assert not output.exists()
    assert not list(tmp_path.glob("*.mp4"))


@pytest.mark.parametrize(
    ("bits", "picture_count"),
    [
        pytest.param("0" * _UNBOUNDED_LEADING_ZEROS + "1", None, id="first-mb-in-slice"),
        # first_mb_in_slice = 0 decodes, so the picture still counts.
        pytest.param("1" + "0" * _UNBOUNDED_LEADING_ZEROS + "1", 1, id="slice-type"),
    ],
)
def test_an_overlong_value_never_unescapes_the_rest_of_the_nal(
    monkeypatch: pytest.MonkeyPatch, bits: str, picture_count: int | None
) -> None:
    unescaped_input_lengths: list[int] = []
    remove_emulation_prevention_bytes = h264._remove_emulation_prevention_bytes

    def record_unescape(ebsp: Iterable[int]) -> bytes:
        ebsp_bytes = bytes(ebsp)
        unescaped_input_lengths.append(len(ebsp_bytes))
        return remove_emulation_prevention_bytes(ebsp_bytes)

    monkeypatch.setattr(h264, "_remove_emulation_prevention_bytes", record_unescape)
    stream = b"\x00\x00\x00\x01\x41" + _pack_bits(bits)

    if picture_count is None:
        with pytest.raises(ValueError, match=_TOO_WIDE_VALUE):
            count_h264_pictures(stream)
    else:
        assert count_h264_pictures(stream) == picture_count
    with pytest.raises(ValueError, match=_TOO_WIDE_VALUE):
        scan_picture_coding_types(stream)

    assert unescaped_input_lengths
    assert max(unescaped_input_lengths) <= _SLICE_HEADER_HEAD_BYTES


def test_an_incomplete_head_still_retries_over_the_whole_nal() -> None:
    """Both fields fit the width limit, but their escapes outgrow the head.

    first_mb_in_slice = 2**32 - 1 and slice_type = 2**31 - 1 are mostly zero
    bits, so the encoder escapes them well past 16 bytes. Only the full-payload
    retry reaches the end of slice_type and can report its invalid value.
    """
    first_mb_in_slice_bits = "0" * 32 + "1" + "0" * 32
    slice_type_bits = "0" * 31 + "1" + "0" * 31
    payload = _escape_emulation(_pack_bits(first_mb_in_slice_bits + slice_type_bits + "1"))
    assert len(payload) > _SLICE_HEADER_HEAD_BYTES
    stream = b"\x00\x00\x00\x01\x41" + payload

    with pytest.raises(ValueError, match=f"invalid slice_type {2**31 - 1}"):
        scan_picture_coding_types(stream)
    assert count_h264_pictures(stream) == 0
