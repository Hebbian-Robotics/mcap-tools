"""JPEG and PNG camera frames are checked against the pixel limit before decoding."""

from __future__ import annotations

import base64
import json
import struct
import sys
import zlib
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import av
import numpy as np
import pytest
from av.video.codeccontext import VideoCodecContext
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from hebbian_mcap.cli import main as mcap_export
from hebbian_mcap.video import CameraExportLimits, McapMediaError, export_mcap_camera
from mcap.writer import Writer

CAMERA_START_NS = 1_700_000_000_000_000_000
FRAME_TIMES_NS = (0, 1_000_000_000)
# Each side fits JPEG's 16-bit header field; the area exceeds the default limit.
OVERSIZED_SIDE = 5808
COMPRESSED_IMAGE_SCHEMAS = ("foxglove.CompressedImage", "sensor_msgs/msg/CompressedImage")
ROS2_COMPRESSED_IMAGE = "string format\nuint8[] data\n"
JPEG_START_OF_FRAME = b"\xff\xc0"

DecodedFrameSizes = list[list[tuple[int, int]]]


@pytest.fixture
def decoded_frame_sizes(monkeypatch: pytest.MonkeyPatch) -> DecodedFrameSizes:
    """Record the frame sizes returned by every real image decode."""
    calls: DecodedFrameSizes = []
    original_decode = VideoCodecContext.decode

    def decode(self: VideoCodecContext, packet: av.Packet | None = None) -> list[av.VideoFrame]:
        calls.append([])
        frames = original_decode(self, packet)
        calls[-1].extend((frame.width, frame.height) for frame in frames)
        return frames

    monkeypatch.setattr(VideoCodecContext, "decode", decode)
    return calls


def _encoded_image(codec_name: str, width: int, height: int) -> bytes:
    pixel_format = "rgb24" if codec_name == "png" else "yuvj420p"
    encoder = cast(VideoCodecContext, av.CodecContext.create(codec_name, "w"))
    encoder.width, encoder.height, encoder.pix_fmt = width, height, pixel_format
    encoder.time_base = Fraction(1, 25)
    encoder.thread_count = 1
    pixels = np.zeros((height, width, 3), dtype=np.uint8)
    frame = av.VideoFrame.from_ndarray(pixels, format="rgb24").reformat(format=pixel_format)
    frame.pts = 0
    return b"".join(bytes(packet) for packet in (*encoder.encode(frame), *encoder.encode(None)))


def _jpeg_frame_header_offset(jpeg: bytes) -> int:
    return jpeg.index(JPEG_START_OF_FRAME)


def _with_jpeg_dimensions(jpeg: bytes, width: int, height: int) -> bytes:
    offset = _jpeg_frame_header_offset(jpeg)
    return jpeg[: offset + 5] + struct.pack(">HH", height, width) + jpeg[offset + 9 :]


def _with_png_dimensions(png: bytes, width: int, height: int) -> bytes:
    header = png[12:16] + struct.pack(">II", width, height) + png[24:29]
    return png[:12] + header + struct.pack(">I", zlib.crc32(header)) + png[33:]


def _oversized_image(image_format: str) -> bytes:
    if "png" in image_format:
        return _with_png_dimensions(_encoded_image("png", 16, 16), OVERSIZED_SIDE, OVERSIZED_SIDE)
    return _with_jpeg_dimensions(_encoded_image("mjpeg", 16, 16), OVERSIZED_SIDE, OVERSIZED_SIDE)


def _write_messages(path: Path, register: Callable[[Writer], int], payload: bytes) -> None:
    with path.open("wb") as output:
        writer = Writer(output)
        writer.start()
        channel_id = register(writer)
        for timestamp in FRAME_TIMES_NS:
            log_time = CAMERA_START_NS + timestamp
            writer.add_message(channel_id, log_time, payload, log_time)
        writer.finish()


def _json_recording(path: Path, image_format: str, image: bytes, schema_name: str) -> None:
    payload = json.dumps({"format": image_format, "data": base64.b64encode(image).decode()})

    def register(writer: Writer) -> int:
        schema_id = writer.register_schema(schema_name, "jsonschema", b"{}")
        return writer.register_channel("/camera", "json", schema_id)

    _write_messages(path, register, payload.encode())


def _ros2_recording(path: Path, image_format: str, image: bytes) -> None:
    from mcap_ros2.writer import Writer as Ros2Writer

    writer = Ros2Writer(str(path))
    schema = writer.register_msgdef("sensor_msgs/msg/CompressedImage", ROS2_COMPRESSED_IMAGE)
    message = SimpleNamespace(format=image_format, data=image)
    for timestamp in FRAME_TIMES_NS:
        log_time = CAMERA_START_NS + timestamp
        writer.write_message("/camera", schema, message, log_time=log_time, publish_time=log_time)
    writer.finish()


def _protobuf_recording(path: Path, image_format: str, image: bytes) -> None:
    # Generated protobuf modules define these names at runtime, without stubs.
    descriptors = vars(descriptor_pb2)
    field = descriptors["FieldDescriptorProto"]
    file_descriptor = descriptors["FileDescriptorProto"](
        name="foxglove/CompressedImage.proto", package="foxglove", syntax="proto3"
    )
    descriptor = file_descriptor.message_type.add(name="CompressedImage")
    for number, (name, field_type) in enumerate(
        (("format", field.TYPE_STRING), ("data", field.TYPE_BYTES)), 1
    ):
        descriptor.field.add(name=name, number=number, label=field.LABEL_OPTIONAL, type=field_type)
    pool = vars(descriptor_pool)["DescriptorPool"]()
    pool.Add(file_descriptor)
    message_type = vars(message_factory)["GetMessageClass"](
        pool.FindMessageTypeByName("foxglove.CompressedImage")
    )
    schema = descriptors["FileDescriptorSet"](file=[file_descriptor]).SerializeToString()

    def register(writer: Writer) -> int:
        schema_id = writer.register_schema("foxglove.CompressedImage", "protobuf", schema)
        return writer.register_channel("/camera", "protobuf", schema_id)

    payload = message_type(format=image_format, data=image).SerializeToString()
    _write_messages(path, register, payload)


def _assert_rejected_before_decode(
    source: Path, output: Path, decoded_frame_sizes: DecodedFrameSizes
) -> None:
    message = f"camera frame dimensions {OVERSIZED_SIDE}x{OVERSIZED_SIDE} are outside"
    with pytest.raises(McapMediaError, match=message):
        export_mcap_camera(source, output)
    assert decoded_frame_sizes == []
    assert not output.exists()
    assert not list(output.parent.glob(".camera-export-*"))


@pytest.mark.parametrize("schema_name", COMPRESSED_IMAGE_SCHEMAS)
@pytest.mark.parametrize("image_format", ["jpeg", "jpg", "png"])
def test_oversized_json_image_is_rejected_before_decode(
    tmp_path: Path, schema_name: str, image_format: str, decoded_frame_sizes: DecodedFrameSizes
) -> None:
    source = tmp_path / "camera.mcap"
    _json_recording(source, image_format, _oversized_image(image_format), schema_name)
    _assert_rejected_before_decode(source, tmp_path / "camera.mp4", decoded_frame_sizes)


def test_oversized_ros2_cdr_image_is_rejected_before_decode(
    tmp_path: Path, decoded_frame_sizes: DecodedFrameSizes
) -> None:
    source = tmp_path / "ros.mcap"
    _ros2_recording(source, "jpeg", _oversized_image("jpeg"))
    _assert_rejected_before_decode(source, tmp_path / "ros.mp4", decoded_frame_sizes)


def test_oversized_protobuf_image_is_rejected_before_decode(
    tmp_path: Path, decoded_frame_sizes: DecodedFrameSizes
) -> None:
    source = tmp_path / "protobuf.mcap"
    _protobuf_recording(source, "png", _oversized_image("png"))
    _assert_rejected_before_decode(source, tmp_path / "protobuf.mp4", decoded_frame_sizes)


def test_mcap_export_command_rejects_oversized_image_before_decode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    decoded_frame_sizes: DecodedFrameSizes,
) -> None:
    source, output = tmp_path / "cli.mcap", tmp_path / "cli.mp4"
    _json_recording(source, "jpeg", _oversized_image("jpeg"), "foxglove.CompressedImage")
    monkeypatch.setattr(sys, "argv", ["mcap-export", str(source), str(output)])
    with pytest.raises(SystemExit) as exit_error:
        mcap_export()
    assert exit_error.value.code == 2
    assert f"{OVERSIZED_SIDE}x{OVERSIZED_SIDE} are outside" in capsys.readouterr().err
    assert decoded_frame_sizes == []
    assert not output.exists()


def test_decoder_cannot_exceed_the_checked_jpeg_header(
    tmp_path: Path, decoded_frame_sizes: DecodedFrameSizes
) -> None:
    # A small frame header and an empty scan hide the real header from the
    # pre-check; FFmpeg still reaches the 512x512 header and would decode it.
    jpeg = _encoded_image("mjpeg", 512, 512)
    offset = _jpeg_frame_header_offset(jpeg)
    header_end = offset + 2 + int.from_bytes(jpeg[offset + 2 : offset + 4], "big")
    small_header = _with_jpeg_dimensions(jpeg, 64, 64)[offset:header_end]
    hidden = jpeg[:offset] + small_header + b"\xff\xda\x00\x02" + jpeg[offset:]
    source, output = tmp_path / "hidden.mcap", tmp_path / "hidden.mp4"
    _json_recording(source, "jpeg", hidden, "foxglove.CompressedImage")
    with pytest.raises(McapMediaError):
        export_mcap_camera(source, output)
    assert decoded_frame_sizes
    assert all(size == (64, 64) for sizes in decoded_frame_sizes for size in sizes)
    assert not output.exists()


@pytest.mark.parametrize(
    ("image_format", "image"),
    [
        # Widths that are not stride-aligned still export at the exact limit.
        ("png", lambda: _encoded_image("png", 65, 48)),
        ("jpeg", lambda: _encoded_image("mjpeg", 65, 48)),
        # FFmpeg's decoder accepts a repeated start-of-image marker.
        ("jpeg", lambda: b"\xff\xd8" + _encoded_image("mjpeg", 65, 48)),
    ],
    ids=["png", "jpeg", "jpeg-repeated-start-of-image"],
)
def test_images_at_the_pixel_limit_export(
    tmp_path: Path, image_format: str, image: Callable[[], bytes]
) -> None:
    source = tmp_path / "camera.mcap"
    _json_recording(source, image_format, image(), "foxglove.CompressedImage")
    limits = CameraExportLimits(maximum_frame_pixels=65 * 48)
    result = export_mcap_camera(source, tmp_path / "camera.mp4", limits=limits)
    with av.open(result.video_path) as video:
        frames = list(video.decode(video=0))
    assert [(frame.width, frame.height) for frame in frames] == [(65, 48)] * len(FRAME_TIMES_NS)
