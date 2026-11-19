"""Stream one MCAP camera to MP4 while preserving irregular log-time intervals.

Requires the optional ``video`` extra. Decoding is synchronous; callers own
process isolation when a hard native-decoder deadline is required.
"""

from __future__ import annotations

import base64
import json
import math
import os
import tempfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from fractions import Fraction
from pathlib import Path
from struct import error as StructError

import av
import numpy as np
from av.container import OutputContainer
from av.stream import Stream
from av.video.codeccontext import VideoCodecContext
from av.video.stream import VideoStream
from mcap.exceptions import McapError
from mcap.reader import McapReader, make_reader
from mcap.records import Channel, Schema
from mcap_protobuf.decoder import DecoderFactory as ProtobufDecoderFactory
from mcap_ros2.decoder import DecoderFactory as Ros2DecoderFactory

from ._native import (
    COMPRESSED_VIDEO_SCHEMA,
    COMPRESSED_VIDEO_SCHEMA_NAME,
    ROS2_COMPRESSED_VIDEO_SCHEMA,
    ROS2_COMPRESSED_VIDEO_SCHEMA_NAME,
    CompressedVideoCodec,
)
from .h264 import (
    annex_b_nal_offsets_and_types,
    ensure_access_unit_delimiter,
    scan_picture_coding_types,
    split_annex_b_stream,
)

PASSTHROUGH_VIDEO_SCHEMA_NAMES = frozenset(
    {COMPRESSED_VIDEO_SCHEMA_NAME, ROS2_COMPRESSED_VIDEO_SCHEMA_NAME}
)
_NATIVE_VIDEO_SCHEMAS = (
    ("protobuf", "protobuf", COMPRESSED_VIDEO_SCHEMA_NAME, COMPRESSED_VIDEO_SCHEMA),
    ("cdr", "ros2msg", ROS2_COMPRESSED_VIDEO_SCHEMA_NAME, ROS2_COMPRESSED_VIDEO_SCHEMA),
)

MAXIMUM_FRAME_BYTES = 32 * 1024 * 1024
NANOSECONDS_PER_SECOND = 1_000_000_000
VIDEO_TICKS_PER_SECOND = 1_000_000
NANOSECONDS_PER_VIDEO_TICK = NANOSECONDS_PER_SECOND // VIDEO_TICKS_PER_SECOND
MAXIMUM_VIDEO_INTERVAL_TICKS = 2**31 - 1
VIDEO_TIME_BASE = Fraction(1, VIDEO_TICKS_PER_SECOND)
COMPRESSED_IMAGE_SCHEMAS = frozenset(
    {"foxglove.CompressedImage", "sensor_msgs/msg/CompressedImage"}
)
RAW_IMAGE_SCHEMAS = frozenset({"foxglove.RawImage", "sensor_msgs/msg/Image"})
RAW_PIXEL_FORMATS = {
    "rgb8": ("rgb24", 3),
    "bgr8": ("bgr24", 3),
    "rgba8": ("rgba", 4),
    "bgra8": ("bgra", 4),
    "mono8": ("gray", 1),
}


@dataclass(frozen=True)
class CameraExportLimits:
    maximum_frame_pixels: int = 33554432
    maximum_frames_per_second: float = 240.0
    minimum_duration_seconds: float = 0.001
    maximum_duration_seconds: float = 86400.0

    def __post_init__(self) -> None:
        if type(self.maximum_frame_pixels) is not int or self.maximum_frame_pixels <= 0:
            raise ValueError("maximum_frame_pixels must be a positive integer")
        for value in (
            self.maximum_frames_per_second,
            self.minimum_duration_seconds,
            self.maximum_duration_seconds,
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError("video limits must be finite and positive")
        if self.minimum_duration_seconds > self.maximum_duration_seconds:
            raise ValueError("video duration limits are reversed")


class _CameraKind(Enum):
    H264 = auto()
    COMPRESSED_IMAGE = auto()
    RAW_IMAGE = auto()


_CAMERA_KIND_BY_SCHEMA = {
    **dict.fromkeys(PASSTHROUGH_VIDEO_SCHEMA_NAMES, _CameraKind.H264),
    **dict.fromkeys(COMPRESSED_IMAGE_SCHEMAS, _CameraKind.COMPRESSED_IMAGE),
    **dict.fromkeys(RAW_IMAGE_SCHEMAS, _CameraKind.RAW_IMAGE),
}


@dataclass(frozen=True)
class CameraChannel:
    channel: Channel
    schema: Schema

    @property
    def topic(self) -> str:
        return self.channel.topic

    @property
    def schema_name(self) -> str:
        return self.schema.name


class McapMediaError(RuntimeError):
    """The selected camera could not be prepared within the supported profile."""


@dataclass(frozen=True, slots=True)
class PreparedMcapVideo:
    video_path: Path = field(repr=False)
    duration_millis: int
    camera_topic: str = field(repr=False)
    start_timestamp_ns: int


@dataclass(frozen=True, slots=True)
class CameraFrameInterval:
    timestamp_ns: int
    duration_ns: int
    message: object = field(repr=False)


def _field(message: object, name: str) -> object:
    return message.get(name) if isinstance(message, Mapping) else getattr(message, name, None)


def _frame_bytes(message: object) -> bytes:
    payload = _field(message, "data")
    if isinstance(payload, str):
        if len(payload) > MAXIMUM_FRAME_BYTES * 2:
            raise McapMediaError("camera payload exceeds the byte limit")
        payload = base64.b64decode(payload, validate=True)
    if isinstance(payload, np.ndarray) and (payload.ndim != 1 or payload.dtype != np.uint8):
        raise McapMediaError("camera payload must be a one-dimensional byte array")
    if not isinstance(payload, bytes | bytearray | memoryview | list | np.ndarray):
        raise McapMediaError("camera message has no byte payload")
    byte_count = payload.nbytes if isinstance(payload, memoryview | np.ndarray) else len(payload)
    if not 0 < byte_count <= MAXIMUM_FRAME_BYTES:
        raise McapMediaError("camera payload is empty or exceeds the byte limit")
    if isinstance(payload, list) and any(type(byte) is not int for byte in payload):
        raise McapMediaError("camera payload array must contain integer bytes")
    return bytes(payload)


def _select_camera(reader: McapReader, camera_topic: str | None) -> CameraChannel:
    summary = reader.get_summary()
    if summary is None:
        raise McapMediaError("camera export requires an indexed MCAP file")
    cameras = [
        channel
        for channel in summary.channels.values()
        if channel.schema_id in summary.schemas
        and summary.schemas[channel.schema_id].name in _CAMERA_KIND_BY_SCHEMA
    ]
    if camera_topic is None:
        if len(cameras) != 1:
            raise McapMediaError("select exactly one camera topic")
        camera_topic = cameras[0].topic
    channels = [channel for channel in summary.channels.values() if channel.topic == camera_topic]
    if len(channels) != 1 or channels[0] not in cameras:
        raise McapMediaError("camera topic must identify exactly one supported channel")
    channel = channels[0]
    schema = summary.schemas[channel.schema_id]
    return CameraChannel(channel, schema)


def _camera_decoder(selected: CameraChannel) -> Callable[[bytes], object]:
    message_encoding = selected.channel.message_encoding
    schema = selected.schema
    schema_identity = (message_encoding, schema.encoding, schema.name, schema.data)
    if schema_identity in _NATIVE_VIDEO_SCHEMAS:
        return CompressedVideoCodec(*schema_identity).decode
    if message_encoding == "json":
        return json.loads
    for factory in (Ros2DecoderFactory(), ProtobufDecoderFactory()):
        decoder = factory.decoder_for(message_encoding, schema)
        if decoder is not None:
            return decoder
    raise McapMediaError("unsupported camera message encoding")


def _camera_messages(
    reader: McapReader,
    selected: CameraChannel,
    limits: CameraExportLimits,
) -> Iterator[tuple[int, object]]:
    decoder = _camera_decoder(selected)
    maximum_frames = math.ceil(limits.maximum_duration_seconds * limits.maximum_frames_per_second)
    for frame_count, (_, _, message) in enumerate(
        reader.iter_messages(topics=[selected.topic], log_time_order=True), 1
    ):
        if frame_count > maximum_frames:
            raise McapMediaError("camera frame count exceeds export limit")
        yield message.log_time, decoder(message.data)


def _frame_intervals(
    first_message: tuple[int, object],
    messages: Iterator[tuple[int, object]],
    limits: CameraExportLimits,
) -> Iterator[CameraFrameInterval]:
    start_timestamp, previous_message = first_message
    previous_timestamp = start_timestamp
    previous_interval = 0
    frame_count = 1
    maximum_duration_ns = int(limits.maximum_duration_seconds * NANOSECONDS_PER_SECOND)
    for timestamp, message in messages:
        interval = timestamp - previous_timestamp
        if interval <= 0:
            raise McapMediaError("camera log times must be strictly increasing")
        if timestamp - start_timestamp > maximum_duration_ns:
            raise McapMediaError("camera duration exceeds the export limit")
        frame_count += 1
        yield CameraFrameInterval(previous_timestamp - start_timestamp, interval, previous_message)
        previous_timestamp, previous_message, previous_interval = timestamp, message, interval
    final_duration = previous_timestamp - start_timestamp + previous_interval
    if previous_interval == 0:
        raise McapMediaError("camera export requires at least two frames")
    if not (
        limits.minimum_duration_seconds * NANOSECONDS_PER_SECOND
        <= final_duration
        <= maximum_duration_ns
    ):
        raise McapMediaError("camera duration is outside the export limits")
    # Log timestamps measure arrival time and can bunch together during capture.
    # Bound sustained density while preserving those positive timestamp gaps.
    if frame_count * NANOSECONDS_PER_SECOND > limits.maximum_frames_per_second * final_duration:
        raise McapMediaError("average camera frame rate exceeds the export limit")
    yield CameraFrameInterval(
        previous_timestamp - start_timestamp, previous_interval, previous_message
    )


def _require_dimensions(width: int, height: int, limits: CameraExportLimits) -> None:
    if width <= 0 or height <= 0 or width * height > limits.maximum_frame_pixels:
        raise McapMediaError(
            f"camera frame dimensions {width}x{height} are outside the export limits"
        )


def _decode_image(message: object, kind: _CameraKind, limits: CameraExportLimits) -> av.VideoFrame:
    payload = _frame_bytes(message)
    if kind is _CameraKind.COMPRESSED_IMAGE:
        encoding = _field(message, "format")
        if not isinstance(encoding, str):
            raise McapMediaError("compressed image is missing its format")
        if "jpeg" in encoding.lower() or "jpg" in encoding.lower():
            codec_name = "mjpeg"
        elif "png" in encoding.lower():
            codec_name = "png"
        else:
            raise McapMediaError(f"unsupported compressed image format {encoding!r}")
        decoder = av.CodecContext.create(codec_name, "r")
        decoder.thread_count = 1
        frames = decoder.decode(av.Packet(payload))
        if len(frames) != 1 or not isinstance(frames[0], av.VideoFrame):
            raise McapMediaError("compressed image must decode to one video frame")
        frame = frames[0]
        _require_dimensions(frame.width, frame.height, limits)
        return frame.reformat(format="rgb24")
    encoding = _field(message, "encoding")
    width, height, step = (_field(message, name) for name in ("width", "height", "step"))
    if (
        not isinstance(encoding, str)
        or encoding not in RAW_PIXEL_FORMATS
        or type(width) is not int
        or type(height) is not int
        or type(step) is not int
    ):
        raise McapMediaError("unsupported raw image pixel format or dimensions")
    _require_dimensions(width, height, limits)
    pixel_format, channels = RAW_PIXEL_FORMATS[encoding]
    if step < width * channels or len(payload) != step * height:
        raise McapMediaError("raw image stride and dimensions do not match its payload")
    pixels = np.frombuffer(payload, dtype=np.uint8).reshape(height, step)[:, : width * channels]
    shape = (height, width) if channels == 1 else (height, width, channels)
    return av.VideoFrame.from_ndarray(
        np.ascontiguousarray(pixels.reshape(shape)), format=pixel_format
    ).reformat(format="rgb24")


def _h264_packet(
    message: object, limits: CameraExportLimits, parameter_sets: dict[int, bytes]
) -> tuple[av.Packet, tuple[int, int] | None]:
    if _field(message, "format") != "h264":
        raise McapMediaError("compressed video must use the h264 format")
    payload = ensure_access_unit_delimiter(_frame_bytes(message))
    coding = scan_picture_coding_types(payload)
    units = split_annex_b_stream(payload)
    if coding.picture_count != 1 or coding.b_picture_count or len(units) != 1:
        raise McapMediaError("expected one H.264 picture without B-frames per message")
    # Codec configuration persists across access units. Later IDRs may omit
    # SPS/PPS, and an SPS-only update must still pass our dimension limits.
    parameter_sets_changed = False
    nal_offsets = annex_b_nal_offsets_and_types(payload)
    for index, (start_offset, nal_type) in enumerate(nal_offsets):
        if nal_type in (7, 8):
            end_offset = nal_offsets[index + 1][0] if index + 1 < len(nal_offsets) else len(payload)
            parameter_sets[nal_type] = payload[start_offset:end_offset]
            parameter_sets_changed = True
    if set(parameter_sets) != {7, 8}:
        raise McapMediaError("H.264 stream is missing its SPS or PPS")
    dimensions = None
    if parameter_sets_changed:
        parser = av.CodecContext.create("h264", "r")
        parser.parse(parameter_sets[7] + parameter_sets[8] + payload)
        parser.parse(None)
        _require_dimensions(parser.width, parser.height, limits)
        dimensions = (parser.width, parser.height)
    packet = av.Packet(payload)
    packet.is_keyframe = units[0].is_keyframe
    return packet, dimensions


def _write_camera_video(
    output: OutputContainer,
    intervals: Iterator[CameraFrameInterval],
    channel: CameraChannel,
    limits: CameraExportLimits,
) -> int:
    stream: Stream | None = None
    parameter_sets: dict[int, bytes] = {}
    source_dimensions: tuple[int, int] | None = None
    duration_ns = 0
    timestamp_ticks = 0
    camera_kind = _CAMERA_KIND_BY_SCHEMA[channel.schema_name]
    for interval in intervals:
        duration_ns = interval.timestamp_ns + interval.duration_ns
        # Round each absolute endpoint once, then share it with the next PTS.
        # Accumulating rounded intervals would drift on irregular camera timing.
        end_timestamp_ticks = (
            duration_ns + NANOSECONDS_PER_VIDEO_TICK // 2
        ) // NANOSECONDS_PER_VIDEO_TICK
        duration_ticks = end_timestamp_ticks - timestamp_ticks
        if not 0 < duration_ticks <= MAXIMUM_VIDEO_INTERVAL_TICKS:
            raise McapMediaError("camera frame interval is outside the MP4 timing range")
        if camera_kind is _CameraKind.H264:
            packet, dimensions = _h264_packet(interval.message, limits, parameter_sets)
            if stream is None:
                if not packet.is_keyframe or dimensions is None:
                    raise McapMediaError("H.264 recording must start with a configured keyframe")
                source_dimensions = dimensions
                stream = output.add_mux_stream(
                    "h264",
                    width=dimensions[0],
                    height=dimensions[1],
                    time_base=VIDEO_TIME_BASE,
                )
            elif dimensions is not None and dimensions != source_dimensions:
                raise McapMediaError("camera resolution changed during export")
            packet.pts = packet.dts = timestamp_ticks
            packet.time_base = VIDEO_TIME_BASE
            packet.stream = stream
        else:
            frame = _decode_image(interval.message, camera_kind, limits)
            if stream is None:
                stream = output.add_stream(
                    "libx264rgb",
                    rate=30,
                    width=frame.width,
                    height=frame.height,
                    pix_fmt="rgb24",
                    time_base=VIDEO_TIME_BASE,
                    options={
                        "crf": "0",
                        "preset": "ultrafast",
                        "tune": "zerolatency",
                        "x264-params": "bframes=0:repeat-headers=1:aud=1",
                    },
                )
                stream.codec_context.thread_count = 1
            if not isinstance(stream, VideoStream) or (frame.width, frame.height) != (
                stream.width,
                stream.height,
            ):
                raise McapMediaError("camera resolution changed during export")
            frame.pts, frame.time_base = timestamp_ticks, VIDEO_TIME_BASE
            packets = stream.encode(frame)
            if len(packets) != 1:
                raise McapMediaError("image encoder did not return one packet per frame")
            packet = packets[0]
        packet.duration = duration_ticks
        output.mux(packet)
        timestamp_ticks = end_timestamp_ticks
    if (
        isinstance(stream, VideoStream)
        and isinstance(stream.codec_context, VideoCodecContext)
        and stream.encode(None)
    ):
        raise McapMediaError("image encoder returned unexpected delayed frames")
    return (duration_ns + 999_999) // 1_000_000


def _export_camera(
    source_path: Path, output_path: Path, camera_topic: str | None, limits: CameraExportLimits
) -> PreparedMcapVideo:
    with source_path.open("rb") as source:
        reader = make_reader(source, validate_crcs=True)
        channel = _select_camera(reader, camera_topic)
        messages = _camera_messages(reader, channel, limits)
        first_message = next(messages, None)
        if first_message is None:
            raise McapMediaError(f"selected camera {channel.topic!r} has no frames")
        with av.open(
            output_path,
            "w",
            format="mp4",
            options={"video_track_timescale": str(VIDEO_TICKS_PER_SECOND)},
        ) as output:
            duration_millis = _write_camera_video(
                output,
                _frame_intervals(first_message, messages, limits),
                channel,
                limits,
            )
    return PreparedMcapVideo(output_path, duration_millis, channel.topic, first_message[0])


_DEFAULT_EXPORT_LIMITS = CameraExportLimits()


def export_mcap_camera(
    source_path: Path,
    output_path: Path,
    *,
    camera_topic: str | None = None,
    limits: CameraExportLimits = _DEFAULT_EXPORT_LIMITS,
) -> PreparedMcapVideo:
    """Export a selected camera without modifying source or existing destination.

    PTS are MCAP log times relative to the first selected frame, rounded to the
    nearest microsecond. The last frame repeats the preceding positive interval.
    Requires at least two frames with distinct rounded times and gaps no larger
    than 2,147.483647 seconds. H.264 without B-frames is remuxed with lossless AUD
    repair. JPEG/PNG and supported 8-bit raw images are encoded as lossless RGB
    H.264. Metadata includes the selected topic and original first log timestamp.
    """
    source_path = source_path.resolve(strict=True)
    if not source_path.is_file() or source_path.stat().st_size == 0:
        raise McapMediaError("MCAP source must be a nonempty local file")
    if output_path.exists() or output_path.is_symlink():
        raise FileExistsError(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output_path.parent, prefix=".camera-export-") as directory:
        staged_path = Path(directory) / "video.mp4"
        try:
            result = _export_camera(source_path, staged_path, camera_topic, limits)
        except (McapError, StructError) as error:
            raise McapMediaError(f"Invalid MCAP recording: {error}") from error
        except McapMediaError:
            raise
        except (RuntimeError, ValueError) as error:
            raise McapMediaError(
                f"MCAP camera cannot be exported with the requested profile: {error}"
            ) from error
        os.link(staged_path, output_path)
        return replace(result, video_path=output_path)
