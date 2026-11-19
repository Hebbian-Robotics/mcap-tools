"""H.264 Annex B inspection and lossless access-unit delimiter repair."""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from itertools import islice
from typing import NamedTuple

_ANNEX_B_START_CODE = b"\x00\x00\x01"

# NAL unit types (H.264 spec, table 7-1).
_NAL_TYPE_IDR_SLICE = 5
_NAL_TYPE_SPS = 7
_NAL_TYPE_PPS = 8
_NAL_TYPE_ACCESS_UNIT_DELIMITER = 9
# VCL NAL types (coded slice data); an access unit must be delimited before these.
_VCL_NAL_TYPES = frozenset({1, 2, 3, 4, 5})
# Types whose RBSP starts with a slice header. Data partitions B/C (3/4) are
# VCL data but carry no ``first_mb_in_slice`` of their own.
_SLICE_HEADER_NAL_TYPES = frozenset({1, 2, 5})
# How many bytes of a VCL NAL's payload to unescape when reading the slice
# header. ``first_mb_in_slice`` and ``slice_type`` are the first two Exp-Golomb
# values; 16 bytes is enough at any legal macroblock index. A NAL whose slice header needs more is
# caught by the head-too-short fallback in the scan.
_SLICE_HEADER_HEAD_BYTES = 16
# Annex B AUD with primary_pic_type 7 (any I/P/B picture), followed by the
# required RBSP stop bit. x264 writes the narrower ``09 10`` for the streams
# encoders produce; repair deliberately uses the permissive value because it must
# not guess a source picture type. Both spellings are legal, and neither
# changes any slice data.
_ACCESS_UNIT_DELIMITER_NAL = b"\x00\x00\x00\x01\x09\xf0"


@dataclass(frozen=True)
class AccessUnit:
    """One decodable H.264 access unit (one video frame), Annex B format."""

    data: bytes
    is_keyframe: bool  # contains an IDR NAL (type 5)
    has_parameter_sets: bool  # contains SPS (type 7) and PPS (type 8) NALs


def annex_b_nal_offsets_and_types(stream: bytes) -> list[tuple[int, int]]:
    """Return ``(start offset, NAL type)`` pairs from an Annex B stream."""
    # Every (start_offset_including_start_code, nal_type) in stream order.
    # This naive byte scan is sound: inside a NAL payload the encoder inserts
    # an emulation-prevention byte (0x03) after any 00 00 pair that would
    # otherwise be followed by 00/01/02/03, so the sequence 00 00 01 can only
    # ever appear as a genuine start code.
    nal_offsets_and_types: list[tuple[int, int]] = []
    search_offset = 0
    while True:
        code_offset = stream.find(_ANNEX_B_START_CODE, search_offset)
        if code_offset == -1:
            break
        type_byte_offset = code_offset + len(_ANNEX_B_START_CODE)
        if type_byte_offset >= len(stream):
            break
        # A preceding zero byte makes this the 4-byte form 00 00 00 01; the
        # zero belongs to the start code and thus to this NAL's slice.
        has_four_byte_start_code = code_offset > 0 and stream[code_offset - 1] == 0
        nal_start_offset = code_offset - 1 if has_four_byte_start_code else code_offset
        nal_offsets_and_types.append((nal_start_offset, stream[type_byte_offset] & 0x1F))
        search_offset = type_byte_offset
    return nal_offsets_and_types


def nal_header_offset(stream: bytes, nal_start_offset: int) -> int:
    if stream.startswith(b"\x00\x00\x00\x01", nal_start_offset):
        return nal_start_offset + 4
    if stream.startswith(_ANNEX_B_START_CODE, nal_start_offset):
        return nal_start_offset + 3
    raise ValueError(f"invalid Annex B start code at byte {nal_start_offset}")


def _unescape_ebsp_head(ebsp: bytes | memoryview, max_bytes: int) -> bytes:
    """Unescape at most ``max_bytes`` of input for a slice-header read."""
    if max_bytes >= len(ebsp):
        return _remove_emulation_prevention_bytes(ebsp)
    return _remove_emulation_prevention_bytes(islice(ebsp, max(0, max_bytes)))


def _remove_emulation_prevention_bytes(ebsp: Iterable[int]) -> bytes:
    rbsp = bytearray()
    consecutive_zeros = 0
    for byte in ebsp:
        if consecutive_zeros >= 2 and byte == 0x03:
            consecutive_zeros = 0
            continue
        rbsp.append(byte)
        consecutive_zeros = consecutive_zeros + 1 if byte == 0 else 0
    return bytes(rbsp)


class _ExpGolombFailure(StrEnum):
    MISSING_STOP_BIT = "slice header has no complete first_mb_in_slice value"
    TRUNCATED_SUFFIX = "slice header truncates its first_mb_in_slice value"


def _decode_unsigned_exp_golomb_at(
    rbsp: bytes, bit_offset: int
) -> tuple[int, int] | _ExpGolombFailure:
    """Read a value and its next bit offset, or describe where decoding stopped."""
    bit_count = len(rbsp) * 8
    leading_zero_bits = 0
    while True:
        if bit_offset + leading_zero_bits >= bit_count:
            return _ExpGolombFailure.MISSING_STOP_BIT
        byte = rbsp[(bit_offset + leading_zero_bits) // 8]
        if (byte >> (7 - (bit_offset + leading_zero_bits) % 8)) & 1:
            break
        leading_zero_bits += 1
    suffix_start = bit_offset + leading_zero_bits + 1
    suffix_end = suffix_start + leading_zero_bits
    if suffix_end > bit_count:
        return _ExpGolombFailure.TRUNCATED_SUFFIX
    suffix = 0
    for bit_position in range(suffix_start, suffix_end):
        byte = rbsp[bit_position // 8]
        suffix = (suffix << 1) | ((byte >> (7 - bit_position % 8)) & 1)
    return (1 << leading_zero_bits) - 1 + suffix, suffix_end


class _SliceHeader(NamedTuple):
    first_mb_in_slice: int
    # Counting pictures only needs the first field, even when slice_type is truncated.
    slice_type: int | None


def _parse_slice_header(rbsp: bytes) -> _SliceHeader | _ExpGolombFailure:
    first_field = _decode_unsigned_exp_golomb_at(rbsp, 0)
    if isinstance(first_field, _ExpGolombFailure):
        return first_field
    first_mb_in_slice, next_bit_offset = first_field
    slice_type_field = _decode_unsigned_exp_golomb_at(rbsp, next_bit_offset)
    slice_type = None if isinstance(slice_type_field, _ExpGolombFailure) else slice_type_field[0]
    return _SliceHeader(first_mb_in_slice, slice_type)


def _iter_slice_headers(stream: bytes) -> Iterator[_SliceHeader | _ExpGolombFailure]:
    """Read both slice fields, using the full RBSP only when the head is too short."""
    stream_view = memoryview(stream)
    nal_offsets_and_types = annex_b_nal_offsets_and_types(stream)
    for nal_index, (nal_start_offset, nal_type) in enumerate(nal_offsets_and_types):
        if nal_type not in _SLICE_HEADER_NAL_TYPES:
            continue
        nal_end_offset = (
            nal_offsets_and_types[nal_index + 1][0]
            if nal_index + 1 < len(nal_offsets_and_types)
            else len(stream)
        )
        header_offset = nal_header_offset(stream, nal_start_offset)
        slice_payload = stream_view[header_offset + 1 : nal_end_offset]
        header = _parse_slice_header(_unescape_ebsp_head(slice_payload, _SLICE_HEADER_HEAD_BYTES))
        if isinstance(header, _ExpGolombFailure) or header.slice_type is None:
            header = _parse_slice_header(_remove_emulation_prevention_bytes(slice_payload))
        yield header


def count_h264_pictures(stream: bytes) -> int:
    """Count coded pictures in one Annex B payload from its slice headers.

    ``first_mb_in_slice == 0`` marks the first slice of a picture. Counting
    that value distinguishes two pictures from a valid multi-slice picture
    without relying on AUDs, which are precisely what repairable inputs lack.
    """
    picture_count = 0
    for slice_header in _iter_slice_headers(stream):
        if isinstance(slice_header, _ExpGolombFailure):
            raise ValueError(slice_header.value)
        if slice_header.first_mb_in_slice == 0:
            picture_count += 1
    return picture_count


@dataclass(frozen=True)
class PictureCodingScan:
    """Picture coding-type evidence parsed from one Annex B payload's slice headers.

    A picture is B when any of its slices declares ``slice_type`` B (codes 1
    and 6, H.264 spec Table 7-6; 0/5 are P and 2/7 are I). Pictures group on
    ``first_mb_in_slice == 0``, exactly as :func:`count_h264_pictures` groups
    them.
    """

    picture_count: int
    b_picture_count: int
    # A B picture cannot be presented before the next non-B picture arrives,
    # so a run of D consecutive B pictures needs D frames of decoder reorder
    # buffering: the longest such run is the reorder depth this stream needs.
    reorder_depth: int
    # The B pictures after the last non-B picture. With no anchor behind them,
    # a ``-c:v copy`` MP4 remux drops exactly this tail at end of stream
    trailing_b_pictures: int


def scan_picture_coding_types(stream: bytes) -> PictureCodingScan:
    """Classify the pictures in one Annex B payload as B or not B.

    Detection reads slice headers, not AUD ``primary_pic_type`` values: the
    AUD repair path (:func:`ensure_access_unit_delimiter`) deliberately writes
    the permissive type-7 delimiter without knowing the real picture type, so
    only slice headers tell the truth. Data partitions B/C (NAL types 3/4)
    carry no slice header of their own and are skipped, as in
    :func:`count_h264_pictures`. A slice header that cannot be parsed is
    fail-closed as an error: an uncountable slice means the stream's B-frame
    freedom cannot be proven.
    """
    scanner = PictureCodingScanner()
    scanner.update(stream)
    return scanner.result()


@dataclass
class PictureCodingScanner:
    """Accumulate the existing slice-header evidence across access units."""

    picture_count: int = 0
    b_picture_count: int = 0
    reorder_depth: int = 0
    trailing_b_pictures: int = 0
    _picture_is_b: bool | None = None
    _preceding_b_run: int = 0

    def update(self, access_unit: bytes) -> None:
        for slice_header in _iter_slice_headers(access_unit):
            if isinstance(slice_header, _ExpGolombFailure) or slice_header.slice_type is None:
                raise ValueError(
                    "a slice header is incomplete or truncated; picture coding types "
                    "cannot be classified"
                )
            if not 0 <= slice_header.slice_type <= 9:
                raise ValueError(f"invalid slice_type {slice_header.slice_type}")
            if slice_header.first_mb_in_slice == 0:
                self.picture_count += 1
                self._preceding_b_run = self.trailing_b_pictures
                self.trailing_b_pictures = 0
                self._picture_is_b = False
            elif self._picture_is_b is None:
                raise ValueError(
                    "a slice header claims first_mb_in_slice > 0 before any picture starts; "
                    "picture coding types cannot be classified"
                )
            if slice_header.slice_type % 5 == 1 and not self._picture_is_b:
                self._picture_is_b = True
                self.b_picture_count += 1
                self.trailing_b_pictures = self._preceding_b_run + 1
                self.reorder_depth = max(self.reorder_depth, self.trailing_b_pictures)

    def result(self) -> PictureCodingScan:
        return PictureCodingScan(
            picture_count=self.picture_count,
            b_picture_count=self.b_picture_count,
            reorder_depth=self.reorder_depth,
            trailing_b_pictures=self.trailing_b_pictures,
        )


def ensure_access_unit_delimiter(access_unit: bytes) -> bytes:
    """Prepend an H.264 AUD when one access unit has none.

    The caller owns the access-unit boundary (for example, one
    ``foxglove.CompressedVideo`` message is one frame). This function does not
    re-encode: every existing byte is retained after the six-byte delimiter.
    Slice headers prove the message contains exactly one picture, including
    when that picture uses multiple slices. A payload without coded slice data
    or with several pictures is rejected instead of being made to look valid.

    """
    nal_types = [nal_type for _, nal_type in annex_b_nal_offsets_and_types(access_unit)]
    if not any(nal_type in _VCL_NAL_TYPES for nal_type in nal_types):
        raise ValueError("cannot insert an access-unit delimiter: no VCL NAL found")
    picture_count = count_h264_pictures(access_unit)
    if picture_count != 1:
        raise ValueError(
            "cannot insert an access-unit delimiter: "
            f"message contains {picture_count} pictures; exactly one is required"
        )
    if _NAL_TYPE_ACCESS_UNIT_DELIMITER in nal_types:
        return access_unit
    return _ACCESS_UNIT_DELIMITER_NAL + access_unit


def split_annex_b_stream(stream: bytes) -> list[AccessUnit]:
    """Split a raw Annex B H.264 byte stream into access units.

    Requires the stream to contain access-unit delimiter NALs (type 9), as
    produced by ``aud=1``; splits on them. Each unit's ``is_keyframe`` /
    ``has_parameter_sets`` are derived from the NAL types present (5 = IDR,
    7 = SPS, 8 = PPS). NAL types are read from the byte after each start code
    (``00 00 01`` / ``00 00 00 01``); emulation-prevention bytes cannot
    produce false start codes mid-NAL for the type scan.
    """
    nal_offsets_and_types = annex_b_nal_offsets_and_types(stream)

    aud_nal_indices = [
        nal_index
        for nal_index, (_, nal_type) in enumerate(nal_offsets_and_types)
        if nal_type == _NAL_TYPE_ACCESS_UNIT_DELIMITER
    ]
    if not aud_nal_indices:
        raise ValueError(
            "no access-unit delimiter (AUD, NAL type 9) found; splitting requires "
            "a stream encoded with aud=1"
        )
    first_aud_nal_index = aud_nal_indices[0]
    vcl_nal_precedes_first_aud = any(
        nal_type in _VCL_NAL_TYPES for _, nal_type in nal_offsets_and_types[:first_aud_nal_index]
    )
    if vcl_nal_precedes_first_aud:
        raise ValueError(
            "a VCL NAL precedes the first access-unit delimiter; splitting requires "
            "a stream encoded with aud=1"
        )

    # A new access unit begins at each AUD (the AUD stays part of its unit);
    # any leading non-VCL NALs attach to the first unit.
    unit_start_offsets = [nal_offsets_and_types[i][0] for i in aud_nal_indices]
    unit_start_offsets[0] = 0
    unit_end_offsets = [*unit_start_offsets[1:], len(stream)]

    access_units: list[AccessUnit] = []
    # Both the NAL list and the unit boundaries are ascending in offset, so a
    # single pointer walk classifies every unit in O(total NALs) -- a per-unit
    # rescan would be quadratic and takes minutes on hour-long streams.
    nal_walk_index = 0
    for unit_start, unit_end in zip(unit_start_offsets, unit_end_offsets, strict=True):
        nal_types_in_unit: set[int] = set()
        while (
            nal_walk_index < len(nal_offsets_and_types)
            and nal_offsets_and_types[nal_walk_index][0] < unit_end
        ):
            nal_types_in_unit.add(nal_offsets_and_types[nal_walk_index][1])
            nal_walk_index += 1
        access_units.append(
            AccessUnit(
                data=stream[unit_start:unit_end],
                is_keyframe=_NAL_TYPE_IDR_SLICE in nal_types_in_unit,
                has_parameter_sets=(
                    _NAL_TYPE_SPS in nal_types_in_unit and _NAL_TYPE_PPS in nal_types_in_unit
                ),
            )
        )
    return access_units
