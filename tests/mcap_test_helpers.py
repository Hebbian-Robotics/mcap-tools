"""Synthetic MCAP camera fixtures using the official writer."""

from collections.abc import Iterable, Mapping
from pathlib import Path

from hebbian_mcap import COMPRESSED_VIDEO_SCHEMA, encode_compressed_video
from mcap.writer import Writer


def write_compressed_video_mcap(
    path: Path,
    messages: Iterable[tuple[str, int, bytes]],
    *,
    frame_id_by_topic: Mapping[str, str] | None = None,
) -> None:
    with path.open("wb") as output:
        writer = Writer(output)
        writer.start()
        schema = writer.register_schema(
            "foxglove.CompressedVideo", "protobuf", COMPRESSED_VIDEO_SCHEMA
        )
        channels: dict[str, int] = {}
        for topic, timestamp, packet in messages:
            if topic not in channels:
                channels[topic] = writer.register_channel(topic, "protobuf", schema)
            frame_id = (frame_id_by_topic or {}).get(topic, "")
            writer.add_message(
                channels[topic],
                timestamp,
                encode_compressed_video(timestamp, frame_id, packet, "h264"),
                timestamp,
            )
        writer.finish()
