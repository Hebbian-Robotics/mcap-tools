# MCAP Tools: Rust and Python writing and video export

[![CI](https://github.com/Hebbian-Robotics/mcap-tools/actions/workflows/ci.yml/badge.svg)](https://github.com/Hebbian-Robotics/mcap-tools/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/hebbian-mcap)](https://pypi.org/project/hebbian-mcap/)

Write standard MCAP recordings with separate chunk groups for video and telemetry,
encode ROS 2/Foxglove messages, and export camera topics from MCAP to MP4.
Python and Rust share the official Rust MCAP writer through a small grouping
layer. Maintained by Hebbian Robotics; this is an independent Foxglove-compatible
project, not an official Foxglove product.

## Install

```sh
uv add 'hebbian-mcap==0.1.0'
```

Prebuilt CPython 3.11+ wheels support Linux (glibc 2.17+, x86_64/aarch64),
macOS (x86_64/aarch64) and Windows (x86_64), without a Rust installation.
Source builds require Rust 1.92+. The Rust crate is available through a pinned
Git dependency and is not published to crates.io; see the
[Rust crate guide](https://github.com/Hebbian-Robotics/mcap-tools/blob/main/crates/mcap-tools/README.md).

## Write grouped MCAP recordings

```python
from hebbian_mcap import GroupedMcapWriter, NO_SCHEMA_ID

with GroupedMcapWriter("recording.mcap") as writer:
    channel = writer.register_channel("/state", "json", NO_SCHEMA_ID, group="telemetry")
    writer.write_message(channel, 123, b'{"position": 1}')
```

Groups buffer independently, so indexed readers can select camera data without
fetching telemetry chunks, or vice versa. Standard MCAP compression, indexes,
metadata and attachments are preserved. Python path outputs are published
atomically; caller-owned streams stay open.

See the [writer behavior reference](https://github.com/Hebbian-Robotics/mcap-tools/blob/main/docs/behavior.md#grouped-writer) for timestamps,
flush rules and lifecycle. For reading, use the official
[Python MCAP reader](https://mcap.dev/docs/python/mcap-apidoc/mcap.reader).

## Export MCAP camera video to MP4

Install the video extra, then select a camera topic:

```sh
uv add 'hebbian-mcap[video]==0.1.0'
mcap-export recording.mcap front.mp4 --topic /cameras/front/video
```

The equivalent Python API is:

```python
from pathlib import Path
from hebbian_mcap.video import export_mcap_camera

result = export_mcap_camera(
    Path("recording.mcap"), Path("front.mp4"), camera_topic="/cameras/front/video"
)
```

Export requires at least two frames and fixed dimensions. It preserves log-time
gaps, leaves the source unchanged and refuses to overwrite an existing output.
See [video export behavior](https://github.com/Hebbian-Robotics/mcap-tools/blob/main/docs/behavior.md#video-export) for supported formats,
timing and limits.

Only the video extra installs PyAV, NumPy and message decoders. The base Python
writer and Rust crate do not need video codecs. `hebbian_mcap.h264` provides
standard-library-only Annex B inspection and repair helpers.

## References and development

- [MCAP specification](https://mcap.dev/spec): the standard file format.
- [Foxglove CompressedVideo schema](https://docs.foxglove.dev/docs/sdk/schemas/compressed-video): video message layout and codec requirements.
- [Contributing](https://github.com/Hebbian-Robotics/mcap-tools/blob/main/CONTRIBUTING.md): validation, dependency updates and releases.
- [LiveKit Cameras](https://github.com/Hebbian-Robotics/livekit-cameras): browser/native camera streaming and H.264 recording primitives.

[Apache-2.0 license](https://github.com/Hebbian-Robotics/mcap-tools/blob/main/LICENSE). See [NOTICE](https://github.com/Hebbian-Robotics/mcap-tools/blob/main/NOTICE) for schema attribution.
