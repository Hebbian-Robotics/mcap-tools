"""Oversized JPEG and PNG camera messages must be rejected before they are decoded.

These tests fail while export decodes a compressed image and only afterwards
applies CameraExportLimits.maximum_frame_pixels. They pass once width and height
are rejected, without a decoded raster, for every encoding that reaches that path.
"""

from __future__ import annotations

import base64
import json
import math
import resource
import subprocess
import sys
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypedDict, cast

import av
import numpy as np
import pytest
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from hebbian_mcap.video import CameraExportLimits
from mcap.writer import Writer

CAMERA_START_NS = 1_700_000_000_000_000_000
FRAME_TIMESTAMPS_NS = (0, 1_000_000_000)
_COMPRESSED_IMAGE_SCHEMAS = (
    "foxglove.CompressedImage",
    "sensor_msgs/msg/CompressedImage",
)
_ROS2_COMPRESSED_IMAGE = "string format\nuint8[] data\n"


def _side_over_pixel_cap(pixel_cap: int) -> int:
    side = math.ceil(math.sqrt(pixel_cap + 1))
    return ((side + 15) // 16) * 16


def _encoded_image(image_format: str) -> tuple[int, int, bytes]:
    side = _side_over_pixel_cap(CameraExportLimits().maximum_frame_pixels)
    if side * side <= CameraExportLimits().maximum_frame_pixels:
        raise RuntimeError("fixture dimensions do not exceed the pixel cap")
    pixels = np.zeros((side, side, 3), dtype=np.uint8)
    if "png" in image_format:
        frame = av.VideoFrame.from_ndarray(pixels, format="rgb24")
        encoder = av.CodecContext.create("png", "w")
        encoder.pix_fmt = "rgb24"
    else:
        frame = av.VideoFrame.from_ndarray(pixels, format="rgb24").reformat(format="yuvj420p")
        encoder = av.CodecContext.create("mjpeg", "w")
        encoder.pix_fmt = "yuvj420p"
        encoder.time_base = Fraction(1, 25)
        frame.pts = 0
    encoder.width = encoder.height = side
    encoder.thread_count = 1
    payload = b"".join(bytes(packet) for packet in (*encoder.encode(frame), *encoder.encode(None)))
    return side, side, payload


def _minimum_decoded_raster_bytes(image_format: str, width: int, height: int) -> int:
    # Baseline JPEG from this encoder is yuvj420p. PNG is rgb24.
    if "png" in image_format:
        return width * height * 3
    return width * height * 3 // 2


def _write_messages(path: Path, register: Callable[[Writer], int], payloads: list[bytes]) -> None:
    with path.open("wb") as output:
        writer = Writer(output)
        writer.start()
        channel_id = register(writer)
        for timestamp, payload in zip(FRAME_TIMESTAMPS_NS, payloads, strict=True):
            log_time = CAMERA_START_NS + timestamp
            writer.add_message(channel_id, log_time, payload, log_time)
        writer.finish()


def _json_recording(path: Path, schema_name: str, image_format: str, image: bytes) -> None:
    body = json.dumps(
        {"format": image_format, "data": base64.b64encode(image).decode("ascii")}
    ).encode()

    def register(writer: Writer) -> int:
        schema_id = writer.register_schema(schema_name, "jsonschema", b"{}")
        return writer.register_channel("/camera", "json", schema_id)

    _write_messages(path, register, [body, body])


def _ros2_recording(path: Path, image_format: str, image: bytes) -> None:
    from mcap_ros2.writer import Writer as Ros2Writer

    writer = Ros2Writer(str(path))
    schema = writer.register_msgdef("sensor_msgs/msg/CompressedImage", _ROS2_COMPRESSED_IMAGE)
    message = SimpleNamespace(format=image_format, data=image)
    for timestamp in FRAME_TIMESTAMPS_NS:
        log_time = CAMERA_START_NS + timestamp
        writer.write_message("/camera", schema, message, log_time=log_time, publish_time=log_time)
    writer.finish()


def _attr(owner: object, name: str) -> Any:
    return getattr(owner, name)


def _protobuf_recording(path: Path, image_format: str, image: bytes) -> None:
    # Field.Type and Field.Label values from descriptor.proto, as generated in
    # google.protobuf.descriptor_pb2 (protobuf 7.36.2): TYPE_STRING=9,
    # TYPE_BYTES=12, LABEL_OPTIONAL=1.
    file_proto = _attr(descriptor_pb2, "FileDescriptorProto")
    field_proto = _attr(descriptor_pb2, "FieldDescriptorProto")
    file_set = _attr(descriptor_pb2, "FileDescriptorSet")
    file_descriptor = file_proto()
    file_descriptor.name = "foxglove/CompressedImage.proto"
    file_descriptor.package = "foxglove"
    file_descriptor.syntax = "proto3"
    descriptor = file_descriptor.message_type.add()
    descriptor.name = "CompressedImage"
    format_field = descriptor.field.add()
    format_field.name = "format"
    format_field.number = 1
    format_field.label = field_proto.LABEL_OPTIONAL
    format_field.type = field_proto.TYPE_STRING
    data_field = descriptor.field.add()
    data_field.name = "data"
    data_field.number = 2
    data_field.label = field_proto.LABEL_OPTIONAL
    data_field.type = field_proto.TYPE_BYTES
    pool = _attr(descriptor_pool, "DescriptorPool")()
    pool.Add(file_descriptor)
    message_type = _attr(message_factory, "GetMessageClass")(
        pool.FindMessageTypeByName("foxglove.CompressedImage")
    )
    encoded = message_type(format=image_format, data=image).SerializeToString()
    schema = file_set(file=[file_descriptor]).SerializeToString()

    def register(writer: Writer) -> int:
        schema_id = writer.register_schema("foxglove.CompressedImage", "protobuf", schema)
        return writer.register_channel("/camera", "protobuf", schema_id)

    _write_messages(path, register, [encoded, encoded])


def _linux_peak_resident_bytes() -> int:
    # VmHWM is this process's peak resident set, in kilobytes. getrusage
    # ru_maxrss is preserved across exec, so a child inherits its parent's peak.
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("VmHWM is missing from /proc/self/status")


def _maximum_resident_bytes() -> int:
    # Linux getrusage(2) reports ru_maxrss in KiB and keeps it across exec.
    # Current XNU reports bytes. Windows exposes the peak working set through
    # PROCESS_MEMORY_COUNTERS.
    if sys.platform == "linux":
        return _linux_peak_resident_bytes()
    if sys.platform == "win32":
        import ctypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = (
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            )

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        process = ctypes.windll.kernel32.GetCurrentProcess()
        if not ctypes.windll.psapi.GetProcessMemoryInfo(
            process, ctypes.byref(counters), counters.cb
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(counters.PeakWorkingSetSize)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return int(peak)
    return int(peak) * 1024


class _ExportReport(TypedDict):
    error_type: str
    message: str
    exit_code: int | None
    decode_calls: int
    growth_bytes: int
    output_exists: bool
    stderr: str


def _measure(mode: str, source: str, output: str) -> None:
    from av.codec.context import CodecContext

    decode_calls: list[int] = []
    decode_name = "decode"
    original_decode = _attr(CodecContext, decode_name)

    def decode(self: CodecContext, packet: av.Packet | None = None) -> list[av.VideoFrame]:
        decode_calls.append(1)
        return original_decode(self, packet)

    setattr(CodecContext, decode_name, decode)
    before = _maximum_resident_bytes()
    error_type = ""
    message = ""
    exit_code: int | None = None
    try:
        if mode == "export":
            from hebbian_mcap.video import export_mcap_camera

            export_mcap_camera(Path(source), Path(output))
        elif mode == "cli":
            from hebbian_mcap.cli import main

            sys.argv = ["mcap-export", source, output]
            try:
                main()
            except SystemExit as exit_error:
                exit_code = exit_error.code if isinstance(exit_error.code, int) else 1
        else:
            raise ValueError(mode)
    except Exception as error:
        error_type = type(error).__name__
        message = str(error)
    after = _maximum_resident_bytes()
    sys.stdout.write(
        json.dumps(
            {
                "error_type": error_type,
                "message": message,
                "exit_code": exit_code,
                "decode_calls": len(decode_calls),
                "growth_bytes": after - before,
                "output_exists": Path(output).exists(),
            }
        )
    )


def _run_measurement(mode: str, source: Path, output: Path) -> _ExportReport:
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), mode, str(source), str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    report = cast(_ExportReport, json.loads(completed.stdout))
    report["stderr"] = completed.stderr
    return report


def _assert_rejected_before_decode(
    report: _ExportReport, width: int, height: int, image_format: str
) -> None:
    limit_message = f"camera frame dimensions {width}x{height} are outside the export limits"
    observed = f"{report['message']}\n{report['stderr']}"
    raster_bytes = _minimum_decoded_raster_bytes(image_format, width, height)
    assert report["output_exists"] is False
    assert limit_message in observed
    assert report["decode_calls"] == 0, report
    assert report["growth_bytes"] < raster_bytes // 2, report


@pytest.mark.parametrize("schema_name", _COMPRESSED_IMAGE_SCHEMAS)
@pytest.mark.parametrize("image_format", ["jpeg", "jpg", "png"])
def test_default_limits_reject_oversized_jpeg_and_png_before_decode(
    tmp_path: Path, schema_name: str, image_format: str
) -> None:
    width, height, image = _encoded_image(image_format)
    source = tmp_path / "camera.mcap"
    output = tmp_path / "camera.mp4"
    _json_recording(source, schema_name, image_format, image)
    report = _run_measurement("export", source, output)
    _assert_rejected_before_decode(report, width, height, image_format)
    assert report["error_type"] == "McapMediaError"
    assert not output.exists()
    assert not list(tmp_path.glob(".camera-export-*"))


def test_ros2_cdr_compressed_image_is_rejected_before_decode(tmp_path: Path) -> None:
    width, height, jpeg = _encoded_image("jpeg")
    source = tmp_path / "ros.mcap"
    output = tmp_path / "ros.mp4"
    _ros2_recording(source, "jpeg", jpeg)
    report = _run_measurement("export", source, output)
    _assert_rejected_before_decode(report, width, height, "jpeg")
    assert report["error_type"] == "McapMediaError"


def test_protobuf_compressed_image_is_rejected_before_decode(tmp_path: Path) -> None:
    width, height, png = _encoded_image("png")
    source = tmp_path / "protobuf.mcap"
    output = tmp_path / "protobuf.mp4"
    _protobuf_recording(source, "png", png)
    report = _run_measurement("export", source, output)
    _assert_rejected_before_decode(report, width, height, "png")
    assert report["error_type"] == "McapMediaError"


def test_mcap_export_command_rejects_oversized_jpeg_before_decode(tmp_path: Path) -> None:
    width, height, image = _encoded_image("jpeg")
    source = tmp_path / "cli.mcap"
    output = tmp_path / "cli.mp4"
    _json_recording(source, "foxglove.CompressedImage", "jpeg", image)
    report = _run_measurement("cli", source, output)
    _assert_rejected_before_decode(report, width, height, "jpeg")
    assert report["exit_code"] == 2
    assert not output.exists()


if __name__ == "__main__":
    _measure(sys.argv[1], sys.argv[2], sys.argv[3])
