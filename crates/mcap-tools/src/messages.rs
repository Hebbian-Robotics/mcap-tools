//! Standard message definitions for interoperable MCAP recordings.
use prost::Message;
pub mod joint_state;

#[derive(Clone, PartialEq, Message)]
pub struct CompressedVideo {
    #[prost(message, optional, tag = "1")]
    pub timestamp: Option<prost_types::Timestamp>,
    #[prost(string, tag = "2")]
    pub frame_id: String,
    #[prost(bytes = "vec", tag = "3")]
    pub data: Vec<u8>,
    #[prost(string, tag = "4")]
    pub format: String,
}

pub const VIDEO_SCHEMA: &[u8] = include_bytes!("../schemas/CompressedVideo.bin");

/// Encode one Foxglove `CompressedVideo` message. The payload is passed through.
#[must_use]
pub fn encode_compressed_video(
    timestamp_nanoseconds: u64,
    frame_id: &str,
    data: &[u8],
    format: &str,
) -> Vec<u8> {
    CompressedVideo {
        timestamp: Some(prost_types::Timestamp {
            seconds: i64::try_from(timestamp_nanoseconds / 1_000_000_000)
                .expect("u64 nanoseconds fit i64 seconds"),
            nanos: i32::try_from(timestamp_nanoseconds % 1_000_000_000)
                .expect("subsecond nanos fit i32"),
        }),
        frame_id: frame_id.into(),
        data: data.to_vec(),
        format: format.into(),
    }
    .encode_to_vec()
}
