# hebbian-mcap: grouped MCAP writing for Rust

Write standard, indexed MCAP recordings with independently buffered channel
groups. Keep camera video separate from robot state and other telemetry so
indexed readers can select the data they need. Built on the official Rust
`mcap` writer, which owns compression, CRCs, indexes and format serialization.

- `GroupedWriter` and `WriterOptions`: per-group chunk targets, optional
  age-based flushing, zstd or uncompressed chunks, metadata and attachments.
- `messages`: standard Foxglove `CompressedVideo` protobuf and ROS 2
  `sensor_msgs/msg/JointState` CDR encoding.

The crate is available through a pinned Git dependency and is not yet published
to crates.io. See the [repository documentation](https://github.com/Hebbian-Robotics/mcap-tools#readme)
for usage and source, the `hebbian-mcap` Python package on
[PyPI](https://pypi.org/project/hebbian-mcap/), and optional MCAP-to-MP4 video export.
Python and Rust share this writer implementation; the Rust crate does not
require the Python video-export dependencies.

Maintained by Hebbian Robotics under Apache-2.0. This is an independent project,
not an official Foxglove product.
