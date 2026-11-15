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
    /// A fixed-size value of this many bytes.
    Primitive(usize),
    String,
    /// A nested message, named `package/Type`.
    Message(String),
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

fn primitive_size(name: &str) -> Option<usize> {
    match name {
        "bool" | "byte" | "char" | "int8" | "uint8" => Some(1),
        "int16" | "uint16" => Some(2),
        "int32" | "uint32" | "float32" => Some(4),
        "int64" | "uint64" | "float64" => Some(8),
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
            let arity = if bound.is_empty() || bound.starts_with("<=") {
                Arity::Sequence
            } else {
                Arity::Fixed(bound.parse().map_err(|_| {
                    schema_error(format!("malformed array length in {type_text:?}"))
                })?)
            };
            (element, arity)
        }
    };
    let element = element.split_once("<=").map_or(element, |(base, _)| base);
    let kind = if let Some(size) = primitive_size(element) {
        FieldKind::Primitive(size)
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
    ["sec", "nanosec"]
        .into_iter()
        .map(|name| Field {
            name: name.to_owned(),
            kind: FieldKind::Primitive(4),
            arity: Arity::Single,
        })
        .collect()
}

impl MessageDefinitions {
    pub(crate) fn parse(schema_name: &str, schema_text: &str) -> Result<Self, MessageError> {
        let root = short_type_name(schema_name);
        // Recorders may leave these two out because every ROS 2 reader knows them.
        let mut fields_by_type: HashMap<String, Vec<Field>> = HashMap::from([
            ("builtin_interfaces/Time".to_owned(), time_fields()),
            ("builtin_interfaces/Duration".to_owned(), time_fields()),
        ]);
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
            fields_by_type.insert(type_name, parse_fields(definition, &package)?);
        }
        let definitions = Self {
            root,
            fields_by_type,
        };
        definitions.require_resolvable(&definitions.root, &mut Vec::new(), &mut HashSet::new())?;
        Ok(definitions)
    }

    /// Every nested type is defined, and nesting ends: a message cannot contain itself.
    fn require_resolvable<'definitions>(
        &'definitions self,
        type_name: &'definitions str,
        enclosing: &mut Vec<&'definitions str>,
        resolved: &mut HashSet<&'definitions str>,
    ) -> Result<(), MessageError> {
        if resolved.contains(type_name) {
            return Ok(());
        }
        if enclosing.contains(&type_name) {
            return Err(schema_error(format!(
                "message {type_name:?} contains itself"
            )));
        }
        if enclosing.len() >= MAXIMUM_NESTING_DEPTH {
            return Err(schema_error("message definitions nest too deeply"));
        }
        let fields = self
            .fields_by_type
            .get(type_name)
            .ok_or_else(|| schema_error(format!("no definition for message {type_name:?}")))?;
        enclosing.push(type_name);
        for field in fields {
            if let FieldKind::Message(nested) = &field.kind {
                self.require_resolvable(nested, enclosing, resolved)?;
            }
        }
        enclosing.pop();
        resolved.insert(type_name);
        Ok(())
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
        writer: &mut Option<CdrWriter>,
    ) -> Result<(), MessageError> {
        let count = match field.arity {
            Arity::Single => return self.copy_value(&field.kind, reader, writer),
            Arity::Fixed(count) => count,
            Arity::Sequence => {
                let count = reader.read_u32()?;
                if let Some(writer) = writer {
                    writer.write_u32(count);
                }
                usize::try_from(count).unwrap_or(usize::MAX)
            }
        };
        // Every element occupies at least one byte, so a larger count is corrupt.
        if count > reader.remaining() {
            return Err(MessageError::Cdr("array is longer than the message"));
        }
        if let FieldKind::Primitive(size) = field.kind {
            if count > 0 {
                let bytes = reader.take_aligned(size, count.saturating_mul(size))?;
                if let Some(writer) = writer {
                    writer.write_raw(size, bytes);
                }
            }
            return Ok(());
        }
        for _ in 0..count {
            self.copy_value(&field.kind, reader, writer)?;
        }
        Ok(())
    }

    fn copy_value(
        &self,
        kind: &FieldKind,
        reader: &mut CdrReader<'_>,
        writer: &mut Option<CdrWriter>,
    ) -> Result<(), MessageError> {
        match kind {
            FieldKind::Primitive(size) => {
                let bytes = reader.take_aligned(*size, *size)?;
                if let Some(writer) = writer {
                    writer.write_raw(*size, bytes);
                }
            }
            FieldKind::String => {
                let stored = reader.read_bytes()?;
                if let Some(writer) = writer {
                    writer.write_bytes(stored);
                }
            }
            FieldKind::Message(type_name) => {
                let fields = self.fields(type_name);
                if fields.is_empty() {
                    // ROS 2 serializes a message without fields as one placeholder byte.
                    let placeholder = reader.take(1)?;
                    if let Some(writer) = writer {
                        writer.write_raw(1, placeholder);
                    }
                }
                for field in fields {
                    self.copy_field(field, reader, writer)?;
                }
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::{Arity, FieldKind, MessageDefinitions};

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
                ("count", FieldKind::Primitive(4), Arity::Single),
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
