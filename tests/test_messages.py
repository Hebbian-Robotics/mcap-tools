"""Standard decoders must understand messages crossing the Python/Rust boundary."""

from collections.abc import Callable
from typing import Any

import pytest
from hebbian_mcap import (
    COMPRESSED_VIDEO_SCHEMA,
    COMPRESSED_VIDEO_SCHEMA_NAME,
    JOINT_STATE_SCHEMA,
    ROS2_COMPRESSED_VIDEO_SCHEMA,
    ROS2_COMPRESSED_VIDEO_SCHEMA_NAME,
    CompressedVideoCodec,
    encode_compressed_video,
    encode_joint_state,
)
from mcap.records import Schema
from mcap_protobuf.decoder import DecoderFactory as ProtobufDecoderFactory
from mcap_ros2.decoder import DecoderFactory as Ros2DecoderFactory
from mcap_ros2.writer import serialize_dynamic


def _standard_protobuf_video_decoder() -> Callable[[bytes], Any]:
    decoder = ProtobufDecoderFactory().decoder_for(
        "protobuf",
        Schema(1, COMPRESSED_VIDEO_SCHEMA, "protobuf", COMPRESSED_VIDEO_SCHEMA_NAME),
    )
    assert decoder is not None
    return decoder


def _standard_ros2_video_decoder() -> Callable[[bytes], Any]:
    decoder = Ros2DecoderFactory().decoder_for(
        "cdr",
        Schema(1, ROS2_COMPRESSED_VIDEO_SCHEMA, "ros2msg", ROS2_COMPRESSED_VIDEO_SCHEMA_NAME),
    )
    assert decoder is not None
    return decoder


def test_video_preserves_capture_time_and_encoded_payload() -> None:
    decoder = _standard_protobuf_video_decoder()
    timestamp = 1_700_000_000_123_456_789
    message = decoder(encode_compressed_video(timestamp, "left", b"encoded-packet", "h264"))
    assert message.timestamp.seconds == 1_700_000_000
    assert message.timestamp.nanos == 123_456_789
    assert message.frame_id == "left"
    assert message.data == b"encoded-packet"
    assert message.format == "h264"


def test_ros2_video_is_readable_as_standard_ros2_cdr() -> None:
    message = _standard_ros2_video_decoder()(
        encode_compressed_video(3_000_000_007, "left", b"encoded-packet", "h264", "cdr")
    )
    assert (message.timestamp.sec, message.timestamp.nanosec) == (3, 7)
    assert message.frame_id == "left"
    assert bytes(message.data) == b"encoded-packet"
    assert message.format == "h264"


def _protobuf_codec() -> CompressedVideoCodec:
    return CompressedVideoCodec(
        "protobuf", "protobuf", COMPRESSED_VIDEO_SCHEMA_NAME, COMPRESSED_VIDEO_SCHEMA
    )


def _ros2_codec() -> CompressedVideoCodec:
    return CompressedVideoCodec(
        "cdr", "ros2msg", ROS2_COMPRESSED_VIDEO_SCHEMA_NAME, ROS2_COMPRESSED_VIDEO_SCHEMA
    )


def test_video_written_by_standard_encoders_decodes_in_either_encoding() -> None:
    foxglove_message = type(_standard_protobuf_video_decoder()(b""))(
        frame_id="left", data=b"packet", format="h264"
    )
    foxglove_message.timestamp.seconds = 3
    foxglove_message.timestamp.nanos = 7
    ros2_encoder = serialize_dynamic(
        ROS2_COMPRESSED_VIDEO_SCHEMA_NAME, ROS2_COMPRESSED_VIDEO_SCHEMA.decode()
    )[ROS2_COMPRESSED_VIDEO_SCHEMA_NAME]
    ros2_message = ros2_encoder(
        {
            "timestamp": {"sec": 3, "nanosec": 7},
            "frame_id": "left",
            "data": b"packet",
            "format": "h264",
        }
    )
    for codec, message in (
        (_protobuf_codec(), foxglove_message.SerializeToString()),
        (_ros2_codec(), ros2_message),
    ):
        video = codec.decode(message)
        assert video.timestamp_nanoseconds == 3_000_000_007
        assert (video.frame_id, video.data, video.format) == ("left", b"packet", "h264")


def test_protobuf_video_without_a_timestamp_reports_none() -> None:
    message = type(_standard_protobuf_video_decoder()(b""))(data=b"packet", format="h264")
    assert _protobuf_codec().decode(message.SerializeToString()).timestamp_nanoseconds is None


def test_replacing_the_payload_keeps_every_other_field() -> None:
    protobuf_message = _standard_protobuf_video_decoder()(
        _protobuf_codec().replace_data(
            encode_compressed_video(3_000_000_007, "left", b"old", "h264"), b"new"
        )
    )
    assert (protobuf_message.timestamp.seconds, protobuf_message.timestamp.nanos) == (3, 7)
    assert (protobuf_message.frame_id, protobuf_message.data, protobuf_message.format) == (
        "left",
        b"new",
        "h264",
    )
    ros2_message = _standard_ros2_video_decoder()(
        _ros2_codec().replace_data(
            encode_compressed_video(3_000_000_007, "left", b"old", "h264", "cdr"), b"new"
        )
    )
    assert (ros2_message.timestamp.sec, ros2_message.timestamp.nanosec) == (3, 7)
    assert (ros2_message.frame_id, bytes(ros2_message.data), ros2_message.format) == (
        "left",
        b"new",
        "h264",
    )


RECORDER_VIDEO_SCHEMA_NAME = "recorder_msgs/msg/StampedVideo"
RECORDER_VIDEO_SCHEMA = """\
uint8 KIND_COLOR=1
std_msgs/Header header
builtin_interfaces/Time timestamp
string frame_id
string recorder
uint8 kind
float64[] exposure_seconds
uint8[] data
float64 gain
string format
Region[<=2] regions
uint16[3] firmware_version
================================================================================
MSG: std_msgs/Header
builtin_interfaces/Time stamp
string frame_id
================================================================================
MSG: recorder_msgs/Region
string label
int64[2] corner
Empty marker
================================================================================
MSG: recorder_msgs/Empty
"""


@pytest.mark.parametrize("replacement", [b"", b"1", b"12345", b"a much longer payload"])
def test_fields_added_by_a_recorder_survive_a_payload_replacement(replacement: bytes) -> None:
    recorded = {
        "header": {"stamp": {"sec": 1, "nanosec": 2}, "frame_id": "rig"},
        "timestamp": {"sec": 3, "nanosec": 7},
        "frame_id": "left",
        "recorder": "source-recorder",
        "kind": 1,
        "exposure_seconds": [0.004, 0.008],
        "data": b"old",
        "gain": 1.5,
        "format": "h264",
        "regions": [
            {"label": "", "corner": [-4, 9], "marker": {}},
            {"label": "hand", "corner": [2**40, 0], "marker": {}},
        ],
        "firmware_version": [1, 20, 300],
    }
    encode = serialize_dynamic(RECORDER_VIDEO_SCHEMA_NAME, RECORDER_VIDEO_SCHEMA)[
        RECORDER_VIDEO_SCHEMA_NAME
    ]
    codec = CompressedVideoCodec(
        "cdr", "ros2msg", RECORDER_VIDEO_SCHEMA_NAME, RECORDER_VIDEO_SCHEMA.encode()
    )

    video = codec.decode(encode(recorded))
    assert video.timestamp_nanoseconds == 3_000_000_007
    assert (video.frame_id, video.data, video.format) == ("left", b"old", "h264")

    # The standard encoder is the reference for how the rewritten message must be laid out.
    assert codec.replace_data(encode(recorded), replacement) == encode(
        {**recorded, "data": replacement}
    )


@pytest.mark.parametrize(
    ("codec", "message"),
    [
        (_ros2_codec(), b"\x00\x01\x00\x00\x03"),
        (_ros2_codec(), b"\x00\x07\x00\x00" + bytes(32)),
        (_protobuf_codec(), b"\xff\xff\xff"),
    ],
)
def test_undecodable_video_messages_are_rejected(
    codec: CompressedVideoCodec, message: bytes
) -> None:
    with pytest.raises(ValueError):
        codec.decode(message)
    with pytest.raises(ValueError):
        codec.replace_data(message, b"new")


@pytest.mark.parametrize(
    ("message_encoding", "schema_encoding", "schema_name", "schema_data"),
    [
        ("json", "jsonschema", COMPRESSED_VIDEO_SCHEMA_NAME, b"{}"),
        ("cdr", "ros2idl", ROS2_COMPRESSED_VIDEO_SCHEMA_NAME, ROS2_COMPRESSED_VIDEO_SCHEMA),
        ("cdr", "ros2msg", "sensor_msgs/msg/JointState", JOINT_STATE_SCHEMA),
        ("cdr", "ros2msg", "example_msgs/msg/Video", b"other_msgs/Missing value\n"),
    ],
)
def test_channels_that_cannot_carry_compressed_video_are_rejected(
    message_encoding: str, schema_encoding: str, schema_name: str, schema_data: bytes
) -> None:
    with pytest.raises(ValueError):
        CompressedVideoCodec(message_encoding, schema_encoding, schema_name, schema_data)


def test_joint_state_is_readable_as_standard_ros2_cdr() -> None:
    decoder = Ros2DecoderFactory().decoder_for(
        "cdr", Schema(1, JOINT_STATE_SCHEMA, "ros2msg", "sensor_msgs/msg/JointState")
    )
    assert decoder is not None
    message = decoder(
        encode_joint_state(3_000_000_007, "base", ["shoulder", "elbow"], [0.25, -0.5])
    )
    assert (message.header.stamp.sec, message.header.stamp.nanosec) == (3, 7)
    assert message.header.frame_id == "base"
    assert message.name == ["shoulder", "elbow"]
    assert list(message.position) == [0.25, -0.5]
    assert len(message.velocity) == len(message.effort) == 0


@pytest.mark.parametrize(
    ("timestamp", "frame_id", "names", "positions"),
    [
        (2**31 * 1_000_000_000, "base", ["joint"], [1.0]),
        (0, "base", ["joint"], []),
        (0, "base\0suffix", ["joint"], [1.0]),
    ],
)
def test_joint_state_rejects_inputs_that_would_corrupt_the_message(
    timestamp: int, frame_id: str, names: list[str], positions: list[float]
) -> None:
    with pytest.raises(ValueError):
        encode_joint_state(timestamp, frame_id, names, positions)
