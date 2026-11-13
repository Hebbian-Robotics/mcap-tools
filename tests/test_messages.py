"""Standard decoders must understand messages crossing the Python/Rust boundary."""

import pytest
from hebbian_mcap import (
    COMPRESSED_VIDEO_SCHEMA,
    JOINT_STATE_SCHEMA,
    encode_compressed_video,
    encode_joint_state,
)
from mcap.records import Schema
from mcap_protobuf.decoder import DecoderFactory as ProtobufDecoderFactory
from mcap_ros2.decoder import DecoderFactory as Ros2DecoderFactory


def test_video_preserves_capture_time_and_encoded_payload() -> None:
    decoder = ProtobufDecoderFactory().decoder_for(
        "protobuf",
        Schema(1, COMPRESSED_VIDEO_SCHEMA, "protobuf", "foxglove.CompressedVideo"),
    )
    assert decoder is not None
    timestamp = 1_700_000_000_123_456_789
    message = decoder(encode_compressed_video(timestamp, "left", b"encoded-packet", "h264"))
    assert message.timestamp.seconds == 1_700_000_000
    assert message.timestamp.nanos == 123_456_789
    assert message.frame_id == "left"
    assert message.data == b"encoded-packet"
    assert message.format == "h264"


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
