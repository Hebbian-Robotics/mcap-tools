"""Camera export must refuse oversized chunk expansion before allocating it.

These tests fail while export decompresses a chunk, or reads its body, using a
size the recording declares that is larger than the frame budget. They pass
once those reads are rejected first.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import sys
import tracemalloc
from collections.abc import Callable
from fractions import Fraction
from functools import partial
from pathlib import Path

import av
import lz4.frame
import numpy as np
import pytest
import zstandard
from hebbian_mcap.cli import main
from hebbian_mcap.video import (
    MAXIMUM_FRAME_BYTES,
    CameraExportLimits,
    McapMediaError,
    export_mcap_camera,
)
from mcap.data_stream import ReadDataStream
from mcap.reader import make_reader
from mcap.records import Chunk
from mcap.writer import CompressionType, IndexType, Writer

# Twice the per-frame cap: large enough that allocating it is unambiguous, and
# still a small recording on disk because the payload is compressible.
DECLARED_EXPANSION_BYTES = MAXIMUM_FRAME_BYTES * 2
# Headroom above the frame cap for a bounded buffer, and below the declared expansion.
ALLOCATION_BUDGET_BYTES = MAXIMUM_FRAME_BYTES + 8 * 1024 * 1024
READER_RECORD_LIMIT_BYTES = 4 * 2**30
CAMERA_TOPIC = "/cam"


def _png_message() -> bytes:
    encoder = av.CodecContext.create("png", "w")
    encoder.width = encoder.height = 2
    encoder.pix_fmt = "rgb24"
    encoder.time_base = Fraction(1, 1)
    frame = av.VideoFrame.from_ndarray(np.zeros((2, 2, 3), dtype=np.uint8), format="rgb24")
    frame.pts = 0
    packets = [*encoder.encode(frame), *encoder.encode(None)]
    encoded = b"".join(bytes(packet) for packet in packets)
    return json.dumps({"data": base64.b64encode(encoded).decode("ascii"), "format": "png"}).encode()


def _expandable_chunk() -> bytes:
    """One unknown record whose payload makes the chunk the declared size."""
    body = bytearray(DECLARED_EXPANSION_BYTES)
    body[0] = 0xFF
    struct.pack_into("<Q", body, 1, DECLARED_EXPANSION_BYTES - 9)
    return bytes(body)


def _write_camera(
    path: Path,
    *,
    compression: CompressionType = CompressionType.ZSTD,
    index_types: IndexType = IndexType.ALL,
    compressed_chunk: bytes | None = None,
) -> None:
    message = _png_message()

    def replace_chunk(_data: bytes, *_args: object, **_kwargs: object) -> bytes:
        assert compressed_chunk is not None
        return compressed_chunk

    with pytest.MonkeyPatch.context() as patch:
        if compressed_chunk is not None and compression is CompressionType.ZSTD:
            patch.setattr(zstandard, "compress", replace_chunk)
        elif compressed_chunk is not None and compression is CompressionType.LZ4:
            patch.setattr(lz4.frame, "compress", replace_chunk)
        with path.open("wb") as output:
            writer = Writer(output, compression=compression, index_types=index_types)
            writer.start()
            schema_id = writer.register_schema("foxglove.CompressedImage", "jsonschema", b"{}")
            channel_id = writer.register_channel(CAMERA_TOPIC, "json", schema_id)
            writer.add_message(channel_id, 0, message, 0)
            writer.add_message(channel_id, 1_000_000_000, message, 1_000_000_000)
            writer.finish()


def _chunk_start(path: Path) -> int:
    with path.open("rb") as source:
        summary = make_reader(source).get_summary()
    assert summary is not None
    assert len(summary.chunk_indexes) == 1
    return summary.chunk_indexes[0].chunk_start_offset


def _read_chunk(path: Path) -> Chunk:
    with path.open("rb") as source:
        source.seek(_chunk_start(path) + 1 + 8)
        return Chunk.read(ReadDataStream(source))


def _patch_u64(path: Path, offset: int, value: int) -> None:
    raw = bytearray(path.read_bytes())
    struct.pack_into("<Q", raw, offset, value)
    path.write_bytes(raw)


def _data_length_offset(path: Path) -> int:
    chunk_start = _chunk_start(path)
    raw = path.read_bytes()
    body = chunk_start + 9
    compression_length = struct.unpack_from("<I", raw, body + 28)[0]
    return body + 32 + compression_length


def _assert_refused_without_expansion(
    source: Path, output: Path, call: Callable[[], object]
) -> None:
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if tracemalloc.is_tracing():
        tracemalloc.stop()
    tracemalloc.start()
    with pytest.raises(McapMediaError):
        call()
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < ALLOCATION_BUDGET_BYTES
    assert not output.exists()
    assert not list(output.parent.glob(".camera-export-*"))
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest


def test_indexed_zstd_content_size_is_rejected_without_allocating(tmp_path: Path) -> None:
    source = tmp_path / "camera.mcap"
    output = tmp_path / "camera.mp4"
    _write_camera(
        source,
        compressed_chunk=zstandard.ZstdCompressor(level=1).compress(_expandable_chunk()),
    )
    chunk = _read_chunk(source)
    assert chunk.uncompressed_size < ALLOCATION_BUDGET_BYTES
    assert zstandard.frame_content_size(chunk.data) == DECLARED_EXPANSION_BYTES
    assert source.stat().st_size < 1024 * 1024

    _assert_refused_without_expansion(
        source, output, lambda: export_mcap_camera(source, output, camera_topic=CAMERA_TOPIC)
    )


def test_indexed_lz4_content_size_is_rejected_without_allocating(tmp_path: Path) -> None:
    source = tmp_path / "camera.mcap"
    output = tmp_path / "camera.mp4"
    _write_camera(
        source,
        compression=CompressionType.LZ4,
        compressed_chunk=lz4.frame.compress(
            _expandable_chunk(), compression_level=0, store_size=True
        ),
    )
    chunk = _read_chunk(source)
    assert chunk.compression == "lz4"
    assert chunk.uncompressed_size < ALLOCATION_BUDGET_BYTES
    assert lz4.frame.get_frame_info(chunk.data)["content_size"] == DECLARED_EXPANSION_BYTES

    _assert_refused_without_expansion(
        source, output, lambda: export_mcap_camera(source, output, camera_topic=CAMERA_TOPIC)
    )


def test_indexed_unknown_zstd_content_size_uses_the_chunk_size_budget(tmp_path: Path) -> None:
    source = tmp_path / "camera.mcap"
    output = tmp_path / "camera.mp4"
    _write_camera(
        source,
        compressed_chunk=zstandard.ZstdCompressor(level=1, write_content_size=False).compress(
            b"\x00" * 32
        ),
    )
    chunk_start = _chunk_start(source)
    _patch_u64(source, chunk_start + 9 + 16, DECLARED_EXPANSION_BYTES)
    chunk = _read_chunk(source)
    assert zstandard.frame_content_size(chunk.data) == -1
    assert chunk.uncompressed_size == DECLARED_EXPANSION_BYTES

    _assert_refused_without_expansion(
        source, output, lambda: export_mcap_camera(source, output, camera_topic=CAMERA_TOPIC)
    )


def test_unindexed_zstd_content_size_is_rejected_without_allocating(tmp_path: Path) -> None:
    source = tmp_path / "camera.mcap"
    output = tmp_path / "camera.mp4"
    _write_camera(
        source,
        index_types=IndexType.ALL & ~IndexType.CHUNK,
        compressed_chunk=zstandard.ZstdCompressor(level=1).compress(_expandable_chunk()),
    )
    with source.open("rb") as recording:
        summary = make_reader(recording).get_summary()
    assert summary is not None
    assert summary.chunk_indexes == []
    assert [channel.topic for channel in summary.channels.values()] == [CAMERA_TOPIC]

    _assert_refused_without_expansion(
        source, output, lambda: export_mcap_camera(source, output, camera_topic=CAMERA_TOPIC)
    )


def test_indexed_chunk_body_is_rejected_without_allocating_its_declared_length(
    tmp_path: Path,
) -> None:
    source = tmp_path / "camera.mcap"
    output = tmp_path / "camera.mp4"
    _write_camera(source)
    data_length_offset = _data_length_offset(source)
    _patch_u64(source, data_length_offset, DECLARED_EXPANSION_BYTES)
    assert struct.unpack_from("<Q", source.read_bytes(), data_length_offset)[0] == (
        DECLARED_EXPANSION_BYTES
    )
    assert source.stat().st_size < DECLARED_EXPANSION_BYTES

    _assert_refused_without_expansion(
        source, output, lambda: export_mcap_camera(source, output, camera_topic=CAMERA_TOPIC)
    )


def test_indexed_record_longer_than_the_reader_limit_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "camera.mcap"
    output = tmp_path / "camera.mp4"
    _write_camera(source)
    chunk_start = _chunk_start(source)
    _patch_u64(source, chunk_start + 1, READER_RECORD_LIMIT_BYTES + 1)
    assert struct.unpack_from("<Q", source.read_bytes(), chunk_start + 1)[0] == (
        READER_RECORD_LIMIT_BYTES + 1
    )

    _assert_refused_without_expansion(
        source, output, lambda: export_mcap_camera(source, output, camera_topic=CAMERA_TOPIC)
    )


def test_cli_rejects_zstd_content_size_without_allocating(tmp_path: Path) -> None:
    source = tmp_path / "camera.mcap"
    output = tmp_path / "camera.mp4"
    _write_camera(
        source,
        compressed_chunk=zstandard.ZstdCompressor(level=1).compress(_expandable_chunk()),
    )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    argv = sys.argv
    if tracemalloc.is_tracing():
        tracemalloc.stop()
    tracemalloc.start()
    try:
        sys.argv = ["mcap-export", str(source), str(output), "--topic", CAMERA_TOPIC]
        with pytest.raises(SystemExit) as exited:
            main()
    finally:
        sys.argv = argv
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert exited.value.code not in (0, None)
    assert peak < ALLOCATION_BUDGET_BYTES
    assert not output.exists()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest


def _compress_without_content_size(data: bytes, *_args: object, window_log: int) -> bytes:
    parameters = zstandard.ZstdCompressionParameters.from_level(
        3, window_log=window_log, write_content_size=False
    )
    compressor = zstandard.ZstdCompressor(compression_params=parameters).compressobj()
    return compressor.compress(data) + compressor.flush()


@pytest.mark.parametrize(("window_log", "exports"), [(23, True), (27, False)])
def test_zstd_window_larger_than_the_chunk_limit_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, window_log: int, exports: bool
) -> None:
    source = tmp_path / "camera.mcap"
    output = tmp_path / "camera.mp4"
    monkeypatch.setattr(
        zstandard, "compress", partial(_compress_without_content_size, window_log=window_log)
    )
    _write_camera(source)
    chunk = _read_chunk(source)
    assert zstandard.frame_content_size(chunk.data) == -1
    assert zstandard.get_frame_parameters(chunk.data).window_size == 1 << window_log

    if exports:
        export_mcap_camera(source, output, camera_topic=CAMERA_TOPIC)
        assert output.exists()
    else:
        with pytest.raises(McapMediaError, match="zstd"):
            export_mcap_camera(source, output, camera_topic=CAMERA_TOPIC)
        assert not output.exists()


def test_chunk_listed_by_repeated_indexes_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "camera.mcap"
    output = tmp_path / "camera.mp4"
    message = _png_message()
    with source.open("wb") as recording:
        writer = Writer(recording, chunk_size=1)
        writer.start()
        schema_id = writer.register_schema("foxglove.CompressedImage", "jsonschema", b"{}")
        channel_id = writer.register_channel(CAMERA_TOPIC, "json", schema_id)
        writer.add_message(channel_id, 0, message, 0)
        writer.add_message(channel_id, 1_000_000_000, message, 1_000_000_000)
        # The writer keeps finished chunk indexes privately until it writes the summary.
        chunk_indexes = vars(writer)["_Writer__chunk_indices"]
        chunk_indexes.extend([chunk_indexes[0]] * 1000)
        writer.finish()
    with source.open("rb") as recording:
        summary = make_reader(recording).get_summary()
    assert summary is not None
    assert len({index.chunk_start_offset for index in summary.chunk_indexes}) == 2
    assert len(summary.chunk_indexes) == 1002

    with pytest.raises(McapMediaError, match="more than once"):
        export_mcap_camera(source, output, camera_topic=CAMERA_TOPIC)
    assert not output.exists()


def test_overlapping_chunks_buffer_at_most_the_chunk_limit(tmp_path: Path) -> None:
    """Each chunk spans the remaining ones, so its late frame waits for all of them."""
    source = tmp_path / "camera.mcap"
    message = _png_message() + b" " * 500
    message_record_bytes = 31 + len(message)
    chunk_count = 8
    with source.open("wb") as recording:
        writer = Writer(recording, chunk_size=2 * message_record_bytes - 1)
        writer.start()
        schema_id = writer.register_schema("foxglove.CompressedImage", "jsonschema", b"{}")
        channel_id = writer.register_channel(CAMERA_TOPIC, "json", schema_id)
        for chunk in range(chunk_count):
            for second in (chunk, 2 * chunk_count - 1 - chunk):
                writer.add_message(channel_id, second * 1_000_000_000, message, 0)
        writer.finish()
    with source.open("rb") as recording:
        summary = make_reader(recording).get_summary()
    assert summary is not None
    assert len(summary.chunk_indexes) == chunk_count

    small_limits = CameraExportLimits(maximum_chunk_bytes=4 * len(message))
    with pytest.raises(McapMediaError, match="overlapping camera chunks"):
        export_mcap_camera(source, tmp_path / "refused.mp4", limits=small_limits)
    assert not (tmp_path / "refused.mp4").exists()

    result = export_mcap_camera(source, tmp_path / "camera.mp4")
    with av.open(result.video_path) as video:
        timestamps = [frame.time for frame in video.decode(video=0)]
    assert timestamps == [float(second) for second in range(2 * chunk_count)]
