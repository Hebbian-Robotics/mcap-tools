//! Plain ROS 2 CDR: a 4-byte encapsulation header, then fields aligned to their
//! size counted from the byte after it.
use super::MessageError;

const LITTLE_ENDIAN_HEADER: [u8; 4] = [0, 1, 0, 0];
const TRUNCATED: MessageError = MessageError::Cdr("field extends past the end of the message");

pub(crate) struct CdrWriter {
    bytes: Vec<u8>,
    big_endian: bool,
}

impl CdrWriter {
    pub(crate) fn new() -> Self {
        Self {
            bytes: LITTLE_ENDIAN_HEADER.to_vec(),
            big_endian: false,
        }
    }

    /// A writer that keeps the header and byte order of the message `reader` reads.
    pub(crate) fn matching(reader: &CdrReader<'_>) -> Self {
        Self {
            bytes: reader.header.to_vec(),
            big_endian: reader.big_endian,
        }
    }

    pub(crate) fn into_bytes(self) -> Vec<u8> {
        self.bytes
    }

    fn align(&mut self, size: usize) {
        while !(self.bytes.len() - 4).is_multiple_of(size) {
            self.bytes.push(0);
        }
    }

    /// Append bytes already in this writer's byte order.
    pub(crate) fn write_raw(&mut self, alignment: usize, bytes: &[u8]) {
        self.align(alignment);
        self.bytes.extend_from_slice(bytes);
    }

    pub(crate) fn write_i32(&mut self, value: i32) {
        self.write_u32(value.cast_unsigned());
    }

    pub(crate) fn write_u32(&mut self, value: u32) {
        let bytes = if self.big_endian {
            value.to_be_bytes()
        } else {
            value.to_le_bytes()
        };
        self.write_raw(4, &bytes);
    }

    /// Callers check that lengths fit `u32` before encoding.
    pub(crate) fn write_length(&mut self, length: usize) {
        self.write_u32(u32::try_from(length).expect("length checked before encoding"));
    }

    pub(crate) fn write_string(&mut self, value: &str) {
        self.write_length(value.len() + 1);
        self.bytes.extend_from_slice(value.as_bytes());
        self.bytes.push(0);
    }

    pub(crate) fn write_bytes(&mut self, values: &[u8]) {
        self.write_length(values.len());
        self.bytes.extend_from_slice(values);
    }

    pub(crate) fn write_doubles(&mut self, values: &[f64]) {
        self.write_length(values.len());
        for value in values {
            let bytes = if self.big_endian {
                value.to_be_bytes()
            } else {
                value.to_le_bytes()
            };
            self.write_raw(8, &bytes);
        }
    }
}

#[derive(Clone)]
pub(crate) struct CdrReader<'message> {
    header: [u8; 4],
    body: &'message [u8],
    position: usize,
    big_endian: bool,
}

impl<'message> CdrReader<'message> {
    pub(crate) fn new(message: &'message [u8]) -> Result<Self, MessageError> {
        let (header, body) = message
            .split_first_chunk::<4>()
            .ok_or(MessageError::Cdr("message is shorter than its header"))?;
        let big_endian = match (header[0], header[1]) {
            (0, 0) => true,
            (0, 1) => false,
            _ => return Err(MessageError::Cdr("unsupported encapsulation")),
        };
        Ok(Self {
            header: *header,
            body,
            position: 0,
            big_endian,
        })
    }

    pub(crate) fn remaining(&self) -> usize {
        self.body.len().saturating_sub(self.position)
    }

    pub(crate) fn take(&mut self, length: usize) -> Result<&'message [u8], MessageError> {
        let end = self
            .position
            .checked_add(length)
            .filter(|end| *end <= self.body.len())
            .ok_or(TRUNCATED)?;
        let bytes = &self.body[self.position..end];
        self.position = end;
        Ok(bytes)
    }

    pub(crate) fn take_aligned(
        &mut self,
        alignment: usize,
        length: usize,
    ) -> Result<&'message [u8], MessageError> {
        self.position = self
            .position
            .checked_next_multiple_of(alignment)
            .ok_or(TRUNCATED)?;
        self.take(length)
    }

    pub(crate) fn read_u32(&mut self) -> Result<u32, MessageError> {
        let bytes = self
            .take_aligned(4, 4)?
            .try_into()
            .expect("take returned four bytes");
        Ok(if self.big_endian {
            u32::from_be_bytes(bytes)
        } else {
            u32::from_le_bytes(bytes)
        })
    }

    pub(crate) fn read_i32(&mut self) -> Result<i32, MessageError> {
        Ok(self.read_u32()?.cast_signed())
    }

    /// A length prefix and the bytes it counts, as stored.
    pub(crate) fn read_bytes(&mut self) -> Result<&'message [u8], MessageError> {
        let length = usize::try_from(self.read_u32()?).map_err(|_| TRUNCATED)?;
        self.take(length)
    }
}

/// The text of a stored CDR string. Serializers differ on whether an empty
/// string stores its terminator.
pub(crate) fn string_text(stored: &[u8]) -> Result<String, MessageError> {
    let text = stored.split_last().map_or(stored, |(_, text)| text);
    String::from_utf8(text.to_vec()).map_err(|_| MessageError::Cdr("string is not UTF-8"))
}
