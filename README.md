# MCAP Tools: Rust and Python MCAP writing and video export

[![CI](https://github.com/Hebbian-Robotics/mcap-tools/actions/workflows/ci.yml/badge.svg)](https://github.com/Hebbian-Robotics/mcap-tools/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/hebbian-mcap)](https://pypi.org/project/hebbian-mcap/)

Write indexed **MCAP recordings** from Rust or Python, keep camera video and
telemetry in separate chunk groups, encode standard ROS 2 and Foxglove messages,
and export camera topics **from MCAP to MP4**. The `hebbian-mcap` package uses a
shared Rust core built on the official MCAP writer, with Python bindings through
PyO3 and an optional video-export CLI.

Maintained by Hebbian Robotics for robotics data collection and other MCAP
workflows. Outputs use the standard MCAP format and work with standard readers.
This is an independent project, not an official Foxglove product.

## Choose an MCAP tool

| Task | API or command |
| --- | --- |
| Write grouped MCAP chunks from Python | `hebbian_mcap.GroupedMcapWriter` — [install](#installation-and-development) |
| Write MCAP from Rust | `hebbian_mcap::GroupedWriter` — [crate source](crates/mcap-tools/src/lib.rs) |
| Encode standard camera and joint messages | Foxglove `CompressedVideo` protobuf and ROS 2 `JointState` CDR |
| Export an MCAP camera topic to MP4 | `mcap-export` CLI or `export_mcap_camera` — [video export](#mcap-to-mp4-camera-video-export) |
| Inspect or repair H.264 Annex B access units | `hebbian_mcap.h264`, without video-codec dependencies |

## Grouped MCAP writer

The `hebbian-mcap` Rust crate and Python package share one Rust implementation:

- Independently buffered channel groups, with per-group chunk targets.
- Indexed zstd or uncompressed chunks, metadata, and attachments.
- Foxglove CompressedVideo protobuf and ROS 2 JointState CDR encoding.
- Atomic publication for Python path outputs; caller-owned binary streams stay open.

```python
from hebbian_mcap import GroupedMcapWriter, NO_SCHEMA_ID

with GroupedMcapWriter("recording.mcap") as writer:
    channel = writer.register_channel("/state", "json", NO_SCHEMA_ID, group="telemetry")
    writer.write_message(channel, 123, b'{"position": 1}')
```

Times are nanoseconds since the Unix epoch. Publish time defaults to log time.
Grouping keeps high-volume camera video separate from smaller telemetry topics,
so indexed readers can select the groups they need. It changes physical write
order; indexed readers recover time order.
Messages retain their original sequence and timestamps. Groups flush in registration
order at finish, or when they exceed their configured size. Rust callers can also
bound buffer age; age is checked on writes, not by a background timer.

Adding metadata or attachments flushes waiting messages first.

The upstream MCAP writer owns compression, CRCs, indexes, and footer serialization.
A small buffering layer isolates groups and can be replaced by upstream grouping
support without changing the public API. Upstream may store schema/channel records
in chunks with no messages; consumers should use message indexes for selective reads.

## Installation and development

Install the Python package from PyPI:

```sh
uv add 'hebbian-mcap==0.1.0'
```

Prebuilt CPython 3.11+ wheels support Linux (glibc 2.17+, x86_64/aarch64),
macOS (x86_64/aarch64), and Windows (x86_64). These installs do not need Rust.
Source builds require Rust 1.92+. See [CONTRIBUTING.md](CONTRIBUTING.md) for
native development and release instructions.

The Rust crate is available through a Git dependency pinned to a revision; it is
not yet published to crates.io.

The Python module is `hebbian_mcap`; the Rust library is `hebbian_mcap`.
The official Python `mcap` reader remains compatible and is not replaced.

## MCAP to MP4 camera video export

Export a selected camera topic from an MCAP file to MP4 using the Python API or
`mcap-export` command. Install the optional video dependencies:

```sh
uv add 'hebbian-mcap[video]==0.1.0'
```

```python
from pathlib import Path
from hebbian_mcap.video import export_mcap_camera

result = export_mcap_camera(
    Path("recording.mcap"), Path("front.mp4"), camera_topic="/cameras/front/video"
)
```

```sh
mcap-export recording.mcap front.mp4 --topic /cameras/front/video
```

The exporter reads standard Foxglove video/image and ROS 2 camera schemas using
JSON, protobuf, or CDR messages. H.264 without B-frames is remuxed, with lossless
AUD repair when needed. Supported raw RGB/gray and JPEG/PNG frames are encoded as
lossless RGB H.264. Original log-time gaps are preserved to microsecond precision;
the final frame repeats the preceding interval. At least two frames are required.
Select an exact topic when more than one camera is declared. Source recordings
and existing destination files are preserved on failure.

`CameraExportLimits` bounds frame dimensions, duration and average frame rate.
Native decoding is synchronous; callers own process isolation when they need a
hard deadline. `PreparedMcapVideo` reports the selected topic, original first log
time, exported duration and output path.

Only the `[video]` extra installs PyAV, NumPy and MCAP message decoders. The Rust
crate and default Python writer do not depend on video codecs. The H.264 inspection
helpers in `hebbian_mcap.h264` use only the Python standard library.

## Related camera streaming tools

[LiveKit Cameras](https://github.com/Hebbian-Robotics/livekit-cameras) provides
browser and native camera streaming, a room viewer, latest-frame snapshots and
H.264 recording primitives for LiveKit applications.

Licensed under [Apache-2.0](LICENSE); see [NOTICE](NOTICE) for schema attribution.
