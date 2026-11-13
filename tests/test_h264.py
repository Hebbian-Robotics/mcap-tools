"""H.264 picture boundaries, coding types and delimiter repair contracts."""

import pytest
from hebbian_mcap.h264 import (
    AccessUnit,
    PictureCodingScan,
    _remove_emulation_prevention_bytes,
    _unescape_ebsp_head,
    count_h264_pictures,
    ensure_access_unit_delimiter,
    nal_header_offset,
    scan_picture_coding_types,
    split_annex_b_stream,
)

_ANNEX_B_4_BYTE_START_CODE = b"\x00\x00\x00\x01"
_IDR_NAL_HEADER = b"\x65"


def _slice_header_rbsp(first_mb_in_slice: int, slice_type_code: int) -> bytes:
    """Pack first_mb_in_slice and slice_type as consecutive Exp-Golomb values."""

    def exp_golomb_bits(value: int) -> str:
        return "0" * ((value + 1).bit_length() - 1) + bin(value + 1)[2:]

    bits = exp_golomb_bits(first_mb_in_slice) + exp_golomb_bits(slice_type_code) + "1"
    bits += "0" * (-len(bits) % 8)
    return bytes(int(bits[index : index + 8], 2) for index in range(0, len(bits), 8))


def test_scan_measures_reorder_depth_and_trailing_tail() -> None:
    non_idr_nal = b"\x00\x00\x00\x01\x41"
    stream = b"".join(
        non_idr_nal + _slice_header_rbsp(0, slice_type_code)
        for slice_type_code in (0, 1, 1, 0, 1)  # P, B, B, P, B in decode order
    )

    scan = scan_picture_coding_types(stream)

    assert scan == PictureCodingScan(
        picture_count=5, b_picture_count=3, reorder_depth=2, trailing_b_pictures=1
    )


def test_scan_refuses_an_unparseable_slice_header() -> None:
    # An all-zero RBSP never terminates the first Exp-Golomb value, so the
    # slice header cannot be classified and the scan must fail closed.
    unparseable_slice = b"\x00\x00\x00\x01\x41\x00"

    with pytest.raises(ValueError, match="cannot be classified"):
        scan_picture_coding_types(unparseable_slice)


def test_split_rejects_garbage_without_aud() -> None:
    with pytest.raises(ValueError, match="aud=1"):
        split_annex_b_stream(b"\xde\xad\xbe\xef" * 32)
    # A VCL NAL (IDR, type 5) before any AUD must also be rejected.
    idr_before_any_aud = b"\x00\x00\x00\x01\x65" + bytes(16)
    with pytest.raises(ValueError, match="aud=1"):
        split_annex_b_stream(idr_before_any_aud)


def test_ensure_aud_is_lossless_and_idempotent() -> None:
    undelimited_keyframe = b"".join(
        b"\x00\x00\x00\x01"
        + bytes([nal_type])
        + (b"\x80payload" if nal_type == 0x65 else b"payload")
        for nal_type in (0x67, 0x68, 0x65)
    )

    repaired = ensure_access_unit_delimiter(undelimited_keyframe)

    assert repaired.endswith(undelimited_keyframe)
    assert len(repaired) == len(undelimited_keyframe) + 6
    assert ensure_access_unit_delimiter(repaired) == repaired
    assert split_annex_b_stream(repaired) == [
        AccessUnit(data=repaired, is_keyframe=True, has_parameter_sets=True)
    ]


def test_ensure_aud_accepts_multiple_slices_for_one_picture() -> None:
    multi_slice_keyframe = b"".join(
        [
            b"\x00\x00\x00\x01\x67payload",
            b"\x00\x00\x00\x01\x68payload",
            b"\x00\x00\x00\x01\x65\x80first-slice",
            b"\x00\x00\x00\x01\x65\x40second-slice",
        ]
    )

    repaired = ensure_access_unit_delimiter(multi_slice_keyframe)

    assert repaired.endswith(multi_slice_keyframe)


def test_ensure_aud_rejects_multiple_pictures_without_auds() -> None:
    two_pictures = b"".join(
        [
            b"\x00\x00\x00\x01\x65\x80first-picture",
            b"\x00\x00\x00\x01\x41\x80second-picture",
        ]
    )

    with pytest.raises(ValueError, match="message contains 2 pictures"):
        ensure_access_unit_delimiter(two_pictures)


def test_ensure_aud_rejects_payload_without_coded_slice_data() -> None:
    parameter_sets_only = b"".join(
        b"\x00\x00\x00\x01" + bytes([nal_type]) + b"payload" for nal_type in (0x67, 0x68)
    )
    with pytest.raises(ValueError, match="no VCL NAL"):
        ensure_access_unit_delimiter(parameter_sets_only)


def test_unescape_ebsp_head_matches_full_unescape_for_the_prefix() -> None:
    """The head-only unescape must produce the same prefix the full unescape
    does, so the slice-header readers see identical bytes for the bytes they
    actually read. An emulation-prevention 0x03 consumed by the full pass
    after the cut is irrelevant: the prefix ends at the cut either way."""
    payload = b"\x00\x00\x00\x01\x65" + bytes(range(256)) * 4
    full = _remove_emulation_prevention_bytes(payload)
    for max_bytes in (8, 16, 32, 64, 128, len(payload), len(payload) + 100):
        head = _unescape_ebsp_head(payload, max_bytes)
        if max_bytes >= len(payload):
            assert head == full
        else:
            # The head may be slightly shorter than max_bytes when the
            # unescape consumes a 0x03 after the 00 00 pair -- that is
            # correct emulation-prevention behavior, and the full pass has
            # the same property.
            assert head == full[: len(head)]


def test_unescape_ebsp_head_with_max_bytes_at_or_above_length_returns_full_unescape() -> None:
    """Asking for the whole NAL (or more) must be a fast path to the full
    unescape, with no behavioral difference. The scan uses this when a
    VCL NAL is shorter than the slice-header head budget, which is
    common for very small frames."""
    payload = b"\x00\x00\x03\x01\x02"
    assert _unescape_ebsp_head(payload, len(payload)) == _remove_emulation_prevention_bytes(payload)
    assert _unescape_ebsp_head(payload, len(payload) + 100) == _remove_emulation_prevention_bytes(
        payload
    )


def test_scan_and_count_agree_on_multi_slice_and_escape_heavy_streams() -> None:
    """Count consistently across a multi-slice picture and a stream whose
    emulation-prevention triples
    straddle the 16-byte head boundary. Both readers run one walk now;
    any future divergence between them fails here."""
    # Two pictures, the first split into two slices (first_mb_in_slice 0, 1).
    multi_slice_stream = b"".join(
        b"\x00\x00\x00\x01\x41" + _slice_header_rbsp(first_mb, slice_type)
        for first_mb, slice_type in ((0, 2), (1, 2), (0, 0))
    )
    assert scan_picture_coding_types(multi_slice_stream).picture_count == 2
    assert count_h264_pictures(multi_slice_stream) == 2

    def escape_prevention_bytes(rbsp: bytes) -> bytes:
        """Encode one RBSP the way an encoder would: 0x03 after each 00 00
        pair followed by a byte at or below 0x03."""
        encoded = bytearray()
        consecutive_zeros = 0
        for byte in rbsp:
            if consecutive_zeros >= 2 and byte <= 0x03:
                encoded.append(0x03)
                consecutive_zeros = 0
            encoded.append(byte)
            consecutive_zeros = consecutive_zeros + 1 if byte == 0 else 0
        return bytes(encoded)

    # first_mb_in_slice = 2**70 encodes as 70 leading zero bits, so its
    # header starts with eight 0x00 bytes and needs 141 bits in total: the
    # escapes push the header past what a 16-byte head can unescape, and
    # only the full-payload fallback can decode it. It appears as the second
    # slice of a picture, since a picture's first slice always carries 0.
    wide_rbsp = _slice_header_rbsp(2**70, 7)
    assert wide_rbsp.startswith(b"\x00" * 8)
    escape_stream = b"".join(
        b"\x00\x00\x00\x01\x41" + escape_prevention_bytes(rbsp)
        for rbsp in (
            _slice_header_rbsp(0, 7),
            wide_rbsp,
            _slice_header_rbsp(0, 7),
        )
    )
    assert scan_picture_coding_types(escape_stream).picture_count == 2
    assert count_h264_pictures(escape_stream) == 2
    assert scan_picture_coding_types(escape_stream).b_picture_count == 0


def test_a_truncated_slice_type_still_counts_but_cannot_be_classified() -> None:
    """The two readers disagree about which field failing matters, and the
    shared walk has to keep that disagreement.

    ``0x80`` is ``first_mb_in_slice`` = ue(0) followed by seven zero bits, so
    the first field decodes and the second never terminates.
    ``count_h264_pictures`` reads only the first and must still count the
    picture, exactly as it did when it had its own single-field decoder;
    the scan needs both and must refuse. Collapsing the walk's two
    per-field ``is None`` checks into one would pass every other test here.
    """
    truncated_slice_type = b"\x00\x00\x00\x01\x41\x80"

    assert count_h264_pictures(truncated_slice_type) == 1
    with pytest.raises(ValueError, match="cannot be classified"):
        scan_picture_coding_types(truncated_slice_type)


def test_scan_refuses_a_truncated_slice_with_the_existing_fail_closed_message() -> None:
    """A NAL whose head runs out before both Exp-Golomb values finish must
    still fail closed with the same message the unoptimized path produced.
    The 16-byte head is enough at any legal first_mb_in_slice; the synthetic
    NAL here is engineered to demand more, exercising the fallback path."""
    # first_mb_in_slice = 2^15 needs 15 leading zero bits + 1 + 15 suffix bits
    # = 31 bits. _SLICE_HEADER_HEAD_BYTES = 16 covers 128 bits, so 31 fits,
    # but we deliberately encode a value whose unescaped form exceeds 16
    # bytes to force the fallback. Easiest: stuff the payload with 0x00 so
    # the decoder's "no terminating 1 bit" guard fires regardless of head.
    bad_slice = b"\x00\x00\x00\x01\x41" + b"\x00" * 64

    with pytest.raises(ValueError, match="cannot be classified"):
        scan_picture_coding_types(bad_slice)


def _single_slice_idr(rbsp: bytes) -> bytes:
    """Build one Annex B IDR NAL with an explicitly supplied slice-header RBSP."""
    return _ANNEX_B_4_BYTE_START_CODE + _IDR_NAL_HEADER + rbsp


def test_nal_header_rejects_invalid_annex_b_start_code() -> None:
    with pytest.raises(ValueError, match="invalid Annex B start code at byte 3"):
        nal_header_offset(b"\x00\x00\x00\xff", 3)


@pytest.mark.parametrize(
    ("slice_header_rbsp", "message"),
    [
        # first_mb_in_slice is unsigned Exp-Golomb. An all-zero RBSP has no
        # terminating one bit, so the decoder must reject it as incomplete.
        pytest.param(
            b"\x00", "slice header has no complete first_mb_in_slice value", id="no-complete-value"
        ),
        # 00000100 starts an Exp-Golomb value with five leading zero bits and
        # a one bit, but only two suffix bits remain in the byte. This reaches
        # the truncation guard rather than the no-complete-value guard.
        pytest.param(
            b"\x04", "slice header truncates its first_mb_in_slice value", id="truncated-value"
        ),
    ],
)
def test_ensure_aud_rejects_a_malformed_first_mb_value(
    slice_header_rbsp: bytes, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ensure_access_unit_delimiter(_single_slice_idr(slice_header_rbsp))
