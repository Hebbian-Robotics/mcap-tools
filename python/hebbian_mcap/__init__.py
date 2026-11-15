"""Grouped MCAP writing with a shared Rust core."""

from hebbian_mcap._native import (
    COMPRESSED_VIDEO_SCHEMA,
    COMPRESSED_VIDEO_SCHEMA_NAME,
    JOINT_STATE_SCHEMA,
    ROS2_COMPRESSED_VIDEO_SCHEMA,
    ROS2_COMPRESSED_VIDEO_SCHEMA_NAME,
    CompressedVideo,
    CompressedVideoCodec,
    encode_compressed_video,
    encode_joint_state,
)
from hebbian_mcap._writer import (
    DEFAULT_CHUNK_SIZE_BYTES,
    NO_SCHEMA_ID,
    ChannelId,
    CompressionType,
    GroupedMcapWriter,
    SchemaId,
)

__all__ = [
    "COMPRESSED_VIDEO_SCHEMA",
    "COMPRESSED_VIDEO_SCHEMA_NAME",
    "DEFAULT_CHUNK_SIZE_BYTES",
    "JOINT_STATE_SCHEMA",
    "NO_SCHEMA_ID",
    "ROS2_COMPRESSED_VIDEO_SCHEMA",
    "ROS2_COMPRESSED_VIDEO_SCHEMA_NAME",
    "ChannelId",
    "CompressedVideo",
    "CompressedVideoCodec",
    "CompressionType",
    "GroupedMcapWriter",
    "SchemaId",
    "encode_compressed_video",
    "encode_joint_state",
]
