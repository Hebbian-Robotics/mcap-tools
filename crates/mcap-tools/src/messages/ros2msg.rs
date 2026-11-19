//! ROS 2 `ros2msg` schemas: the message definition, then the definitions it
//! uses, each after a separator line and a `MSG: package/Type` line.
use super::{
    MessageError,
    cdr::{CdrReader, CdrWriter},
};
use std::collections::{HashMap, HashSet};

/// Deeper nesting than any real message; bounds recursion on hostile schemas.
const MAXIMUM_NESTING_DEPTH: usize = 64;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum FieldKind {
    Primitive(PrimitiveKind),
    String,
    /// A nested message, named `package/Type`.
    Message(String),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum PrimitiveKind {
    Uint8,
    Int32,
    Uint32,
    // For fields the codec copies without interpreting the value, only alignment matters.
    Other { size: usize },
}

impl PrimitiveKind {
    fn size(self) -> usize {
        match self {
            Self::Uint8 => 1,
            Self::Int32 | Self::Uint32 => 4,
            Self::Other { size } => size,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Arity {
    Single,
    Fixed(usize),
    Sequence,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Field {
    pub(crate) name: String,
    pub(crate) kind: FieldKind,
    pub(crate) arity: Arity,
}

#[derive(Debug)]
pub(crate) struct MessageDefinitions {
    root: String,
    fields_by_type: HashMap<String, Vec<Field>>,
}

fn schema_error(message: impl Into<String>) -> MessageError {
    MessageError::Schema(message.into())
}

/// `package/msg/Type` and `package/Type` name the same message.
fn short_type_name(name: &str) -> String {
    match (name.split('/').next(), name.rsplit('/').next()) {
        (Some(package), Some(message)) if name.contains('/') => format!("{package}/{message}"),
        _ => name.to_owned(),
    }
}

fn primitive_kind(name: &str) -> Option<PrimitiveKind> {
    match name {
        "uint8" => Some(PrimitiveKind::Uint8),
        "int32" => Some(PrimitiveKind::Int32),
        "uint32" => Some(PrimitiveKind::Uint32),
        "bool" | "byte" | "char" | "int8" => Some(PrimitiveKind::Other { size: 1 }),
        "int16" | "uint16" => Some(PrimitiveKind::Other { size: 2 }),
        "float32" => Some(PrimitiveKind::Other { size: 4 }),
        "int64" | "uint64" | "float64" => Some(PrimitiveKind::Other { size: 8 }),
        _ => None,
    }
}

fn parse_field_type(type_text: &str, package: &str) -> Result<(FieldKind, Arity), MessageError> {
    let (element, arity) = match type_text.split_once('[') {
        None => (type_text, Arity::Single),
        Some((element, suffix)) => {
            let bound = suffix
                .strip_suffix(']')
                .ok_or_else(|| schema_error(format!("malformed array type {type_text:?}")))?;
            let arity = if bound.is_empty() {
                Arity::Sequence
            } else if let Some(maximum) = bound.strip_prefix("<=") {
                maximum
                    .parse::<u32>()
                    .map_err(|_| schema_error(format!("malformed array bound in {type_text:?}")))?;
                Arity::Sequence
            } else {
                Arity::Fixed(bound.parse().map_err(|_| {
                    schema_error(format!("malformed array length in {type_text:?}"))
                })?)
            };
            (element, arity)
        }
    };
    let element = if let Some((base, maximum)) = element.split_once("<=") {
        if !matches!(base, "string" | "wstring") || maximum.parse::<u32>().is_err() {
            return Err(schema_error(format!(
                "malformed string bound in {type_text:?}"
            )));
        }
        base
    } else {
        element
    };
    let kind = if let Some(primitive) = primitive_kind(element) {
        FieldKind::Primitive(primitive)
    } else if element == "string" {
        FieldKind::String
    } else if element == "wstring" {
        return Err(schema_error("wstring fields are not supported"));
    } else if element == "Header" {
        FieldKind::Message("std_msgs/Header".to_owned())
    } else if element.contains('/') {
        FieldKind::Message(short_type_name(element))
    } else {
        FieldKind::Message(format!("{package}/{element}"))
    };
    Ok((kind, arity))
}

fn parse_fields(definition: &[&str], package: &str) -> Result<Vec<Field>, MessageError> {
    let mut fields = Vec::new();
    let mut field_names = HashSet::new();
    for line in definition {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let (type_text, rest) = line
            .split_once(char::is_whitespace)
            .ok_or_else(|| schema_error(format!("field {line:?} has no name")))?;
        let rest = rest.trim_start();
        let name_length = rest
            .find(|character: char| !(character.is_ascii_alphanumeric() || character == '_'))
            .unwrap_or(rest.len());
        let (name, after_name) = rest.split_at(name_length);
        if name.is_empty() {
            return Err(schema_error(format!("field {line:?} has no name")));
        }
        if after_name.trim_start().starts_with('=') {
            // A constant is part of the definition but not of the serialized message.
            continue;
        }
        if !field_names.insert(name) {
            return Err(schema_error(format!("duplicate field {name:?}")));
        }
        let (kind, arity) = parse_field_type(type_text, package)?;
        fields.push(Field {
            name: name.to_owned(),
            kind,
            arity,
        });
    }
    Ok(fields)
}

fn time_fields() -> Vec<Field> {
    [
        ("sec", PrimitiveKind::Int32),
        ("nanosec", PrimitiveKind::Uint32),
    ]
    .into_iter()
    .map(|(name, kind)| Field {
        name: name.to_owned(),
        kind: FieldKind::Primitive(kind),
        arity: Arity::Single,
    })
    .collect()
}

impl MessageDefinitions {
    pub(crate) fn parse(schema_name: &str, schema_text: &str) -> Result<Self, MessageError> {
        let root = short_type_name(schema_name);
        // Recorders may leave these two out because every ROS 2 reader knows them.
        let mut fields_by_type = HashMap::new();
        let lines: Vec<&str> = schema_text.lines().collect();
        let is_separator =
            |line: &&str| line.trim().len() >= 3 && line.trim().bytes().all(|byte| byte == b'=');
        for (index, section) in lines.split(is_separator).enumerate() {
            let named = section
                .iter()
                .position(|line| line.trim_start().starts_with("MSG:"));
            let (type_name, definition) = match named {
                Some(position) => (
                    short_type_name(section[position].trim_start()["MSG:".len()..].trim()),
                    &section[position + 1..],
                ),
                None if index == 0 => (root.clone(), section),
                None => return Err(schema_error("a nested definition has no MSG: line")),
            };
            let package = type_name.split('/').next().unwrap_or_default().to_owned();
            let fields = parse_fields(definition, &package)?;
            if fields_by_type.insert(type_name.clone(), fields).is_some() {
                return Err(schema_error(format!(
                    "duplicate definition for message {type_name:?}"
                )));
            }
        }
        for builtin in ["builtin_interfaces/Time", "builtin_interfaces/Duration"] {
            fields_by_type
                .entry(builtin.to_owned())
                .or_insert_with(time_fields);
        }
        let definitions = Self {
            root,
            fields_by_type,
        };
        definitions.nesting_depth(&definitions.root, &mut Vec::new(), &mut HashMap::new())?;
        Ok(definitions)
    }

    /// Every nested type is defined, and nesting ends: a message cannot contain itself.
    fn nesting_depth<'definitions>(
        &'definitions self,
        type_name: &'definitions str,
        enclosing: &mut Vec<&'definitions str>,
        depths: &mut HashMap<&'definitions str, usize>,
    ) -> Result<usize, MessageError> {
        if enclosing.contains(&type_name) {
            return Err(schema_error(format!(
                "message {type_name:?} contains itself"
            )));
        }
        if enclosing.len() >= MAXIMUM_NESTING_DEPTH {
            return Err(schema_error("message definitions nest too deeply"));
        }
        if let Some(&depth) = depths.get(type_name) {
            if enclosing.len() + depth > MAXIMUM_NESTING_DEPTH {
                return Err(schema_error("message definitions nest too deeply"));
            }
            return Ok(depth);
        }
        let fields = self
            .fields_by_type
            .get(type_name)
            .ok_or_else(|| schema_error(format!("no definition for message {type_name:?}")))?;
        enclosing.push(type_name);
        let mut depth = 1;
        for field in fields {
            if let FieldKind::Message(nested) = &field.kind {
                depth = depth.max(1 + self.nesting_depth(nested, enclosing, depths)?);
            }
        }
        enclosing.pop();
        depths.insert(type_name, depth);
        Ok(depth)
    }

    pub(crate) fn root_fields(&self) -> &[Field] {
        self.fields(&self.root)
    }

    pub(crate) fn fields(&self, type_name: &str) -> &[Field] {
        self.fields_by_type
            .get(type_name)
            .map_or(&[], Vec::as_slice)
    }

    /// Advance `reader` past one field, appending it to `writer` unchanged.
    pub(crate) fn copy_field(
        &self,
        field: &Field,
        reader: &mut CdrReader<'_>,
        mut writer: Option<&mut CdrWriter>,
    ) -> Result<(), MessageError> {
        let count = match field.arity {
            Arity::Single => return self.copy_value(&field.kind, reader, writer),
            Arity::Fixed(count) => count,
            Arity::Sequence => {
                let count = reader.read_u32()?;
                if let Some(writer) = writer.as_deref_mut() {
                    writer.write_u32(count);
                }
                usize::try_from(count)
                    .map_err(|_| MessageError::Cdr("array length exceeds platform limits"))?
            }
        };
        // Every element occupies at least one byte, so a larger count is corrupt.
        if count > reader.remaining() {
            return Err(MessageError::Cdr("array is longer than the message"));
        }
        if let FieldKind::Primitive(primitive) = field.kind {
            let size = primitive.size();
            if count > 0 {
                let length = count
                    .checked_mul(size)
                    .ok_or(MessageError::Cdr("array length exceeds platform limits"))?;
                let bytes = reader.take_aligned(size, length)?;
                if let Some(writer) = writer {
                    writer.write_raw(size, bytes);
                }
            }
            return Ok(());
        }
        for _ in 0..count {
            self.copy_value(&field.kind, reader, writer.as_deref_mut())?;
        }
        Ok(())
    }

    fn copy_value(
        &self,
        kind: &FieldKind,
        reader: &mut CdrReader<'_>,
        mut writer: Option<&mut CdrWriter>,
    ) -> Result<(), MessageError> {
        match kind {
            FieldKind::Primitive(primitive) => {
                let size = primitive.size();
                let bytes = reader.take_aligned(size, size)?;
                if let Some(writer) = writer {
                    writer.write_raw(size, bytes);
                }
            }
            FieldKind::String => {
                let stored = reader.read_bytes()?;
                if let Some(writer) = writer {
                    writer.write_bytes(stored)?;
                }
            }
            FieldKind::Message(type_name) => {
                let fields = self.fields(type_name);
                if fields.is_empty() {
                    // ROS 2 serializes a message without fields as one placeholder byte.
                    let placeholder = reader.take(1)?;
                    if let Some(writer) = writer.as_deref_mut() {
                        writer.write_raw(1, placeholder);
                    }
                }
                for field in fields {
                    self.copy_field(field, reader, writer.as_deref_mut())?;
                }
            }
        }
        Ok(())
    }
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    reason = "Test assertions fail immediately on unexpected outcomes"
)]
mod tests {
    use super::{Arity, FieldKind, MessageDefinitions, PrimitiveKind};

    #[test]
    fn constants_comments_and_defaults_are_not_fields() {
        let definitions = MessageDefinitions::parse(
            "example_msgs/msg/Example",
            "# a comment\nuint8 KIND=1\nstring LABEL = \"a#b\"\nint32 count 7 # default\nstring<=8[<=4] names\n",
        )
        .unwrap();
        let fields: Vec<_> = definitions
            .root_fields()
            .iter()
            .map(|field| (field.name.as_str(), field.kind.clone(), field.arity))
            .collect();
        assert_eq!(
            fields,
            [
                (
                    "count",
                    FieldKind::Primitive(PrimitiveKind::Int32),
                    Arity::Single
                ),
                ("names", FieldKind::String, Arity::Sequence),
            ]
        );
    }

    #[test]
    fn undefined_and_self_containing_messages_are_rejected() {
        assert!(MessageDefinitions::parse("a/msg/A", "b/Missing value\n").is_err());
        assert!(MessageDefinitions::parse("a/msg/A", "A[] children\n").is_err());
    }
}
