# MCAP tools

Tools for working with standard MCAP files, built on the official Rust MCAP
library. Maintained by Hebbian Robotics. This project does not extend the MCAP
format and is not an official Foxglove project.

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
Grouping changes physical write order; indexed readers recover time order.
Messages retain their original sequence and timestamps. Groups flush in registration
order at finish, or when they exceed their configured size. Rust callers can also
bound buffer age; age is checked on writes, not by a background timer.

Adding metadata or attachments flushes waiting messages first.

The upstream MCAP writer owns compression, CRCs, indexes, and footer serialization.
A small buffering layer isolates groups and can be replaced by upstream grouping
support without changing the public API. Upstream may store schema/channel records
in chunks with no messages; consumers should use message indexes for selective reads.

## Installation and development

Until packages are released, use a Git dependency pinned to a commit. Python source
installs require a Rust toolchain; CI builds and smoke-tests native wheels for Linux, macOS,
and Windows. See [CONTRIBUTING.md](CONTRIBUTING.md).

```sh
uv add 'hebbian-mcap @ git+https://github.com/Hebbian-Robotics/mcap-tools@COMMIT'
```

Packages are not yet published to PyPI or crates.io. A source installation needs
Rust 1.92+. Publish the Python wheels before a consumer declares a released
PyPI dependency on this package.

The Python module is `hebbian_mcap`; the Rust library is `hebbian_mcap`.
The official Python `mcap` reader remains compatible and is not replaced.

## Optional camera export

Install `hebbian-mcap[video]` to export a selected camera to MP4:

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

Licensed under [Apache-2.0](LICENSE); see [NOTICE](NOTICE) for schema attribution.
