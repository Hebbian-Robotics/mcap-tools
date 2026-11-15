//! Standard message definitions for interoperable MCAP recordings.
use std::str::FromStr;
mod cdr;
mod compressed_video;
pub mod joint_state;
mod ros2msg;

pub use compressed_video::{
    CompressedVideo, CompressedVideoCodec, ROS2_VIDEO_SCHEMA, ROS2_VIDEO_SCHEMA_NAME, VIDEO_SCHEMA,
    VIDEO_SCHEMA_NAME, encode_compressed_video,
};

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum MessageError {
    #[error("unsupported message encoding {0:?}; expected \"protobuf\" or \"cdr\"")]
    UnsupportedEncoding(String),
    #[error("unsupported schema: {0}")]
    Schema(String),
    #[error("malformed protobuf message: {0}")]
    Protobuf(#[from] prost::DecodeError),
    #[error("malformed protobuf message: {0}")]
    ProtobufWire(&'static str),
    #[error("malformed CDR message: {0}")]
    Cdr(&'static str),
    #[error("{0}")]
    OutOfRange(&'static str),
}

/// How an MCAP channel serializes its messages, named as the channel's
/// `message_encoding` names it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MessageEncoding {
    Protobuf,
    Cdr,
}

impl FromStr for MessageEncoding {
    type Err = MessageError;

    fn from_str(name: &str) -> Result<Self, Self::Err> {
        match name {
            "protobuf" => Ok(Self::Protobuf),
            "cdr" => Ok(Self::Cdr),
            other => Err(MessageError::UnsupportedEncoding(other.to_owned())),
        }
    }
}
