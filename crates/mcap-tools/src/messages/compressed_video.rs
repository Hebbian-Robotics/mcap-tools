//! `CompressedVideo` as Foxglove protobuf and as ROS 2 CDR.
use super::{
    MessageEncoding, MessageError,
    cdr::{CdrReader, CdrWriter, string_text},
    ros2msg::{Arity, Field, FieldKind, MessageDefinitions, PrimitiveKind},
};
use prost::{
    Message,
    encoding::{DecodeContext, WireType, decode_key, encode_key, encode_varint, skip_field},
};
use std::str::FromStr;

pub const VIDEO_SCHEMA_NAME: &str = "foxglove.CompressedVideo";
pub const VIDEO_SCHEMA: &[u8] = include_bytes!("../../schemas/CompressedVideo.bin");

pub const ROS2_VIDEO_SCHEMA_NAME: &str = "foxglove_msgs/msg/CompressedVideo";
/// The message definition an MCAP `ros2msg` schema carries.
pub const ROS2_VIDEO_SCHEMA: &[u8] = b"builtin_interfaces/Time timestamp
string frame_id
uint8[] data
string format
================================================================================
MSG: builtin_interfaces/Time
int32 sec
uint32 nanosec
";

const PROTOBUF_DATA_FIELD_NUMBER: u32 = 3;

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

impl CompressedVideo {
    /// A message stamped `timestamp_nanoseconds` after the Unix epoch.
    #[must_use]
    #[allow(
        clippy::expect_used,
        reason = "Dividing u64 nanoseconds bounds seconds below i64::MAX and the remainder below one billion"
    )]
    pub fn stamped(timestamp_nanoseconds: u64, frame_id: &str, data: &[u8], format: &str) -> Self {
        Self {
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
    }

    /// Serialize with the standard schema: [`VIDEO_SCHEMA`] for protobuf,
    /// [`ROS2_VIDEO_SCHEMA`] for CDR. CDR has no absent timestamp, so a missing
    /// one is written as zero.
    pub fn encode_as(&self, encoding: MessageEncoding) -> Result<Vec<u8>, MessageError> {
        match encoding {
            MessageEncoding::Protobuf => Ok(self.encode_to_vec()),
            MessageEncoding::Cdr => self.encode_as_cdr(),
        }
    }

    fn encode_as_cdr(&self) -> Result<Vec<u8>, MessageError> {
        let timestamp = self.timestamp.unwrap_or_default();
        let seconds = i32::try_from(timestamp.seconds).map_err(|_| {
            MessageError::OutOfRange("CompressedVideo timestamp exceeds ROS 2 seconds range")
        })?;
        let nanoseconds = u32::try_from(timestamp.nanos).map_err(|_| {
            MessageError::OutOfRange("CompressedVideo timestamp has negative nanoseconds")
        })?;
        if nanoseconds >= 1_000_000_000 {
            return Err(MessageError::OutOfRange(
                "CompressedVideo timestamp nanoseconds must be below one second",
            ));
        }
        let mut cdr = CdrWriter::new();
        cdr.write_i32(seconds);
        cdr.write_u32(nanoseconds);
        cdr.write_string(&self.frame_id)?;
        cdr.write_bytes(&self.data)?;
        cdr.write_string(&self.format)?;
        Ok(cdr.into_bytes())
    }
}

/// Encode one Foxglove `CompressedVideo` message. The payload is passed through.
#[must_use]
pub fn encode_compressed_video(
    timestamp_nanoseconds: u64,
    frame_id: &str,
    data: &[u8],
    format: &str,
) -> Vec<u8> {
    CompressedVideo::stamped(timestamp_nanoseconds, frame_id, data, format).encode_to_vec()
}

/// What a top-level CDR field contributes to a decoded `CompressedVideo`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum FieldRole {
    Timestamp,
    FrameId,
    Data,
    Format,
    Other,
}

#[derive(Debug)]
enum ChannelLayout {
    Protobuf,
    Cdr {
        definitions: MessageDefinitions,
        roles: Vec<FieldRole>,
    },
}

/// Reads and rewrites the `CompressedVideo` messages of one MCAP channel.
///
/// A CDR channel is read through its own schema, so a recorder's extra fields
/// are located and kept. A protobuf channel is read by Foxglove field numbers.
#[derive(Debug)]
pub struct CompressedVideoCodec(ChannelLayout);

struct CdrVideoFields<'message> {
    timestamp: Option<(i32, u32)>,
    frame_id: &'message [u8],
    data: &'message [u8],
    format: &'message [u8],
}

fn cdr_field_role(definitions: &MessageDefinitions, field: &Field) -> FieldRole {
    match (field.name.as_str(), &field.kind, field.arity) {
        ("data", FieldKind::Primitive(PrimitiveKind::Uint8), Arity::Sequence) => FieldRole::Data,
        ("format", FieldKind::String, Arity::Single) => FieldRole::Format,
        ("frame_id", FieldKind::String, Arity::Single) => FieldRole::FrameId,
        ("timestamp", FieldKind::Message(type_name), Arity::Single)
            if matches!(
                definitions.fields(type_name),
                [seconds, nanoseconds]
                    if seconds.name == "sec"
                        && seconds.kind == FieldKind::Primitive(PrimitiveKind::Int32)
                        && seconds.arity == Arity::Single
                        && nanoseconds.name == "nanosec"
                        && nanoseconds.kind == FieldKind::Primitive(PrimitiveKind::Uint32)
                        && nanoseconds.arity == Arity::Single
            ) =>
        {
            FieldRole::Timestamp
        }
        _ => FieldRole::Other,
    }
}

impl CompressedVideoCodec {
    /// Resolve the codec for a channel from its `message_encoding` and its schema
    /// record's encoding, name and data.
    pub fn for_channel(
        message_encoding: &str,
        schema_encoding: &str,
        schema_name: &str,
        schema_data: &[u8],
    ) -> Result<Self, MessageError> {
        match MessageEncoding::from_str(message_encoding)? {
            MessageEncoding::Protobuf => Ok(Self(ChannelLayout::Protobuf)),
            MessageEncoding::Cdr => {
                if schema_encoding != "ros2msg" {
                    return Err(MessageError::Schema(format!(
                        "CDR video needs a ros2msg schema, not {schema_encoding:?}"
                    )));
                }
                let schema_text = std::str::from_utf8(schema_data)
                    .map_err(|_| MessageError::Schema("ros2msg schema is not UTF-8".into()))?;
                let definitions = MessageDefinitions::parse(schema_name, schema_text)?;
                let roles: Vec<FieldRole> = definitions
                    .root_fields()
                    .iter()
                    .map(|field| cdr_field_role(&definitions, field))
                    .collect();
                for (role, description) in [
                    (FieldRole::Data, "a `uint8[] data` field"),
                    (FieldRole::Format, "a `string format` field"),
                ] {
                    if !roles.contains(&role) {
                        return Err(MessageError::Schema(format!(
                            "schema {schema_name:?} is not a CompressedVideo: it has no {description}"
                        )));
                    }
                }
                Ok(Self(ChannelLayout::Cdr { definitions, roles }))
            }
        }
    }

    /// Decode one message. Fields the schema does not declare are left empty.
    pub fn decode(&self, message: &[u8]) -> Result<CompressedVideo, MessageError> {
        match &self.0 {
            ChannelLayout::Protobuf => Ok(CompressedVideo::decode(message)?),
            ChannelLayout::Cdr { definitions, roles } => {
                let (fields, _) = walk_cdr_video(definitions, roles, message, None)?;
                let timestamp = fields
                    .timestamp
                    .map(|(seconds, nanoseconds)| {
                        if nanoseconds >= 1_000_000_000 {
                            return Err(MessageError::Cdr(
                                "timestamp nanoseconds must be below one second",
                            ));
                        }
                        Ok(prost_types::Timestamp {
                            seconds: seconds.into(),
                            nanos: nanoseconds.cast_signed(),
                        })
                    })
                    .transpose()?;
                Ok(CompressedVideo {
                    timestamp,
                    frame_id: string_text(fields.frame_id)?,
                    data: fields.data.to_vec(),
                    format: string_text(fields.format)?,
                })
            }
        }
    }

    /// Re-serialize one message with a new payload. Every other field keeps its
    /// stored bytes, including fields outside the standard schema.
    pub fn replace_data(&self, message: &[u8], data: &[u8]) -> Result<Vec<u8>, MessageError> {
        match &self.0 {
            ChannelLayout::Protobuf => replace_protobuf_data(message, data),
            ChannelLayout::Cdr { definitions, roles } => {
                let (_, rewritten) = walk_cdr_video(definitions, roles, message, Some(data))?;
                rewritten.ok_or(MessageError::Cdr("replacement payload was not encoded"))
            }
        }
    }
}

fn walk_cdr_video<'message>(
    definitions: &MessageDefinitions,
    roles: &[FieldRole],
    message: &'message [u8],
    replacement_data: Option<&[u8]>,
) -> Result<(CdrVideoFields<'message>, Option<Vec<u8>>), MessageError> {
    let mut reader = CdrReader::new(message)?;
    let mut writer = replacement_data.map(|data| (data, CdrWriter::matching(&reader)));
    let mut video = CdrVideoFields {
        timestamp: None,
        frame_id: &[],
        data: &[],
        format: &[],
    };
    for (field, role) in definitions.root_fields().iter().zip(roles) {
        let replacement = writer.as_mut().map(|(data, writer)| (*data, writer));
        match role {
            FieldRole::Data => {
                video.data = reader.read_bytes()?;
                if let Some((payload, writer)) = replacement {
                    writer.write_bytes(payload)?;
                }
            }
            FieldRole::FrameId | FieldRole::Format => {
                let stored = reader.read_bytes()?;
                if *role == FieldRole::FrameId {
                    video.frame_id = stored;
                } else {
                    video.format = stored;
                }
                if let Some((_, writer)) = replacement {
                    writer.write_bytes(stored)?;
                }
            }
            FieldRole::Timestamp => {
                let seconds = reader.read_i32()?;
                let nanoseconds = reader.read_u32()?;
                video.timestamp = Some((seconds, nanoseconds));
                if let Some((_, writer)) = replacement {
                    writer.write_i32(seconds);
                    writer.write_u32(nanoseconds);
                }
            }
            FieldRole::Other => {
                definitions.copy_field(
                    field,
                    &mut reader,
                    replacement.map(|(_, writer)| writer),
                )?;
            }
        }
    }
    Ok((video, writer.map(|(_, writer)| writer.into_bytes())))
}

fn replace_protobuf_data(message: &[u8], data: &[u8]) -> Result<Vec<u8>, MessageError> {
    let mut rewritten = Vec::with_capacity(message.len().saturating_add(data.len()));
    let write_data = |rewritten: &mut Vec<u8>| {
        if !data.is_empty() {
            encode_key(
                PROTOBUF_DATA_FIELD_NUMBER,
                WireType::LengthDelimited,
                rewritten,
            );
            encode_varint(data.len() as u64, rewritten);
            rewritten.extend_from_slice(data);
        }
    };
    let mut data_written = false;
    let mut rest = message;
    while !rest.is_empty() {
        let field = rest;
        let (field_number, wire_type) = decode_key(&mut rest)?;
        skip_field(wire_type, field_number, &mut rest, DecodeContext::default())?;
        if field_number != PROTOBUF_DATA_FIELD_NUMBER {
            rewritten.extend_from_slice(&field[..field.len() - rest.len()]);
        } else if wire_type != WireType::LengthDelimited {
            return Err(MessageError::ProtobufWire("data field is not bytes"));
        } else if !data_written {
            write_data(&mut rewritten);
            data_written = true;
        }
    }
    if !data_written {
        write_data(&mut rewritten);
    }
    Ok(rewritten)
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    reason = "Test assertions fail immediately on unexpected outcomes"
)]
mod tests {
    use super::{
        CompressedVideo, CompressedVideoCodec, MessageEncoding, ROS2_VIDEO_SCHEMA,
        ROS2_VIDEO_SCHEMA_NAME, VIDEO_SCHEMA, VIDEO_SCHEMA_NAME,
    };
    use crate::messages::MessageError;

    fn example_video() -> CompressedVideo {
        CompressedVideo {
            timestamp: Some(prost_types::Timestamp {
                seconds: 3,
                nanos: 7,
            }),
            frame_id: "left".into(),
            data: vec![0, 0, 1, 9, 240],
            format: "h264".into(),
        }
    }

    fn standard_codec(encoding: MessageEncoding) -> CompressedVideoCodec {
        match encoding {
            MessageEncoding::Protobuf => CompressedVideoCodec::for_channel(
                "protobuf",
                "protobuf",
                VIDEO_SCHEMA_NAME,
                VIDEO_SCHEMA,
            ),
            MessageEncoding::Cdr => CompressedVideoCodec::for_channel(
                "cdr",
                "ros2msg",
                ROS2_VIDEO_SCHEMA_NAME,
                ROS2_VIDEO_SCHEMA,
            ),
        }
        .unwrap()
    }

    #[test]
    fn every_encoding_round_trips_all_fields() {
        for encoding in [MessageEncoding::Protobuf, MessageEncoding::Cdr] {
            let bytes = example_video().encode_as(encoding).unwrap();
            assert_eq!(
                standard_codec(encoding).decode(&bytes).unwrap(),
                example_video()
            );
        }
    }

    #[test]
    fn big_endian_cdr_keeps_its_byte_order_when_rewritten() {
        let mut bytes = vec![0, 0, 0, 0];
        bytes.extend_from_slice(&3_i32.to_be_bytes());
        bytes.extend_from_slice(&7_u32.to_be_bytes());
        bytes.extend_from_slice(&[0, 0, 0, 5, b'l', b'e', b'f', b't', 0, 0, 0, 0]);
        bytes.extend_from_slice(&[0, 0, 0, 5, 0, 0, 1, 9, 240, 0, 0, 0]);
        bytes.extend_from_slice(&[0, 0, 0, 5, b'h', b'2', b'6', b'4', 0]);
        let codec = standard_codec(MessageEncoding::Cdr);
        assert_eq!(codec.decode(&bytes).unwrap(), example_video());
        let unchanged = codec.replace_data(&bytes, &[0, 0, 1, 9, 240]).unwrap();
        assert_eq!(unchanged, bytes);
    }

    #[test]
    fn replacing_the_payload_keeps_every_other_field() {
        for encoding in [MessageEncoding::Protobuf, MessageEncoding::Cdr] {
            let codec = standard_codec(encoding);
            let original = example_video().encode_as(encoding).unwrap();
            let replaced = codec.replace_data(&original, b"a longer payload").unwrap();
            assert_eq!(
                codec.decode(&replaced).unwrap(),
                CompressedVideo {
                    data: b"a longer payload".to_vec(),
                    ..example_video()
                }
            );
        }
    }

    #[test]
    fn protobuf_fields_outside_the_schema_survive_a_payload_replacement() {
        let codec = standard_codec(MessageEncoding::Protobuf);
        let mut original = example_video()
            .encode_as(MessageEncoding::Protobuf)
            .unwrap();
        let unknown_fields = [0x28, 0x2a, 0x32, 0x01, b'x'];
        original.extend_from_slice(&unknown_fields);
        let replaced = codec.replace_data(&original, b"new").unwrap();
        assert!(replaced.ends_with(&unknown_fields));
        assert_eq!(codec.decode(&replaced).unwrap().data, b"new");
    }

    #[test]
    fn truncated_messages_are_errors() {
        let codec = standard_codec(MessageEncoding::Cdr);
        let bytes = example_video().encode_as(MessageEncoding::Cdr).unwrap();
        for length in 0..bytes.len() - 1 {
            assert!(codec.decode(&bytes[..length]).is_err());
            assert!(codec.replace_data(&bytes[..length], b"new").is_err());
        }
        // A protobuf message cut between fields is a shorter valid message, so cut inside one.
        let codec = standard_codec(MessageEncoding::Protobuf);
        let bytes = example_video()
            .encode_as(MessageEncoding::Protobuf)
            .unwrap();
        assert!(codec.decode(&bytes[..bytes.len() - 1]).is_err());
        assert!(
            codec
                .replace_data(&bytes[..bytes.len() - 1], b"new")
                .is_err()
        );
    }

    #[test]
    fn schemas_without_the_video_fields_are_rejected() {
        let error = CompressedVideoCodec::for_channel(
            "cdr",
            "ros2msg",
            "example_msgs/msg/NotVideo",
            b"string format\nfloat64[] data\n",
        )
        .unwrap_err();
        assert!(matches!(error, MessageError::Schema(_)));
        assert!(matches!(
            CompressedVideoCodec::for_channel(
                "json",
                "jsonschema",
                "foxglove.CompressedVideo",
                b""
            ),
            Err(MessageError::UnsupportedEncoding(_))
        ));
    }
}
