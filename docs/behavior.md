# MCAP tool behavior

This page documents the buffering and export behavior added by this project.
The upstream writer owns format serialization; use the
[MCAP specification](https://mcap.dev/spec) for record layouts and indexes.

## Grouped writer

- Message times are Unix nanoseconds. Publish time defaults to log time;
  sequence numbers and supplied timestamps are preserved.
- Each group has its own chunk target. Groups flush in registration order at
  finish, or when they exceed their size target. Rust callers can set a maximum
  buffer age, checked on writes rather than by a background timer.
- Metadata and attachments flush pending messages before they are written.
- Grouping changes physical message order. Use indexed, log-time-ordered reads
  when time order is required. Schema/channel-only chunks may contain no messages.
- Python file-path outputs are staged and published atomically after success.
  Caller-owned binary streams remain open. Explicitly finish Rust writers;
  dropping a writer abandons buffered messages.

`GroupedWriter` and `WriterOptions` are the Rust entry points;
`GroupedMcapWriter` is the Python adapter. Standard message helpers encode
Foxglove `CompressedVideo` protobuf and ROS 2 `JointState` CDR.

## Video export

The exporter reads Foxglove video/image and ROS 2 camera messages encoded as JSON,
protobuf or CDR. Choose an exact topic when the file declares multiple cameras.

| Input | Output behavior |
| --- | --- |
| H.264 without B-frames | Remux to MP4, repairing access-unit delimiters when needed |
| Supported raw RGB/gray or JPEG/PNG images | Encode lossless RGB H.264 |
| Resolution changes or fewer than two frames | Reject the export |

Log-time gaps are preserved to microsecond precision. The final frame uses the
preceding frame interval. Failed exports preserve the source and existing output.

`CameraExportLimits` bounds dimensions, duration and average frame rate.
`PreparedMcapVideo` reports the selected topic, first log timestamp, duration and
output path. Native decoding is synchronous; applications needing a hard deadline
must provide process isolation.

For schema fields and H.264 packet requirements, see the official
[Foxglove video schema](https://docs.foxglove.dev/docs/sdk/schemas/compressed-video).
