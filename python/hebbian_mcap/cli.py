"""Command-line access to the optional camera exporter."""

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export one MCAP camera to MP4, preserving frame timing."
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--topic", help="Exact camera topic; required for multi-camera recordings")
    arguments = parser.parse_args()
    try:
        from .video import McapMediaError, export_mcap_camera
    except ModuleNotFoundError as error:
        if error.name not in {"av", "numpy", "mcap", "mcap_protobuf", "mcap_ros2"}:
            raise
        parser.error(
            "Video export requires the optional dependencies: install 'hebbian-mcap[video]'."
        )
    try:
        result = export_mcap_camera(
            arguments.source, arguments.output, camera_topic=arguments.topic
        )
    except (McapMediaError, OSError) as error:
        parser.error(str(error))
    print(
        json.dumps(
            {
                "video_path": str(result.video_path),
                "camera_topic": result.camera_topic,
                "start_timestamp_ns": result.start_timestamp_ns,
                "duration_millis": result.duration_millis,
            }
        )
    )


if __name__ == "__main__":
    main()
