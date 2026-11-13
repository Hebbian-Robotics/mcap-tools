//! `sensor_msgs/msg/JointState` as ROS 2 CDR, so Foxglove's 3D and plot panels and
//! Rerun read an arm's joints from the recording without any setup.

/// The message definition an MCAP `ros2msg` schema carries: the message, then the
/// messages it uses, each after a separator line.
pub const SCHEMA: &[u8] = b"std_msgs/Header header
string[] name
float64[] position
float64[] velocity
float64[] effort
================================================================================
MSG: std_msgs/Header
builtin_interfaces/Time stamp
string frame_id
================================================================================
MSG: builtin_interfaces/Time
int32 sec
uint32 nanosec
";

struct CdrBuffer(Vec<u8>);

impl CdrBuffer {
    /// Little-endian CDR: a 4-byte encapsulation header, then fields aligned to their
    /// size counted from the byte after it.
    fn new() -> Self {
        Self(vec![0, 1, 0, 0])
    }

    fn align(&mut self, size: usize) {
        while !(self.0.len() - 4).is_multiple_of(size) {
            self.0.push(0);
        }
    }

    fn write_u32(&mut self, value: u32) {
        self.align(4);
        self.0.extend_from_slice(&value.to_le_bytes());
    }

    fn write_length(&mut self, length: usize) {
        self.write_u32(u32::try_from(length).expect("length checked before encoding"));
    }

    fn write_string(&mut self, value: &str) {
        self.write_length(value.len() + 1);
        self.0.extend_from_slice(value.as_bytes());
        self.0.push(0);
    }

    fn write_doubles(&mut self, values: &[f64]) {
        self.write_length(values.len());
        if !values.is_empty() {
            self.align(8);
        }
        for value in values {
            self.0.extend_from_slice(&value.to_le_bytes());
        }
    }
}

/// One `JointState` stamped `timestamp_nanoseconds` after the Unix epoch, with
/// positions in radians and no velocity or effort.
pub fn encode(
    timestamp_nanoseconds: u64,
    frame_id: &str,
    names: &[String],
    positions: &[f64],
) -> std::io::Result<Vec<u8>> {
    let invalid_input = |message| std::io::Error::new(std::io::ErrorKind::InvalidInput, message);
    let seconds = i32::try_from(timestamp_nanoseconds / 1_000_000_000)
        .map_err(|_| invalid_input("JointState timestamp exceeds ROS 2 signed seconds range"))?;
    if names.len() != positions.len() {
        return Err(invalid_input(
            "JointState names and positions must have equal lengths",
        ));
    }
    for length in std::iter::once(names.len())
        .chain(std::iter::once(frame_id.len().saturating_add(1)))
        .chain(names.iter().map(|name| name.len().saturating_add(1)))
    {
        u32::try_from(length)
            .map_err(|_| invalid_input("JointState field exceeds CDR length range"))?;
    }
    if frame_id.contains('\0') || names.iter().any(|name| name.contains('\0')) {
        return Err(invalid_input(
            "CDR strings cannot contain embedded NUL characters",
        ));
    }
    let mut cdr = CdrBuffer::new();
    cdr.0.extend_from_slice(&seconds.to_le_bytes());
    cdr.write_u32(u32::try_from(timestamp_nanoseconds % 1_000_000_000).unwrap_or(0));
    cdr.write_string(frame_id);
    cdr.write_length(names.len());
    for name in names {
        cdr.write_string(name);
    }
    cdr.write_doubles(positions);
    cdr.write_doubles(&[]);
    cdr.write_doubles(&[]);
    Ok(cdr.0)
}

#[cfg(test)]
mod tests {
    use super::encode;

    #[test]
    fn layout_follows_cdr_alignment() {
        let bytes = encode(3_000_000_007, "x", &["a".to_owned()], &[1.0]).unwrap();
        let mut expected = vec![0, 1, 0, 0]; // little-endian CDR
        expected.extend_from_slice(&3_i32.to_le_bytes()); // stamp.sec
        expected.extend_from_slice(&7_u32.to_le_bytes()); // stamp.nanosec
        expected.extend_from_slice(&[2, 0, 0, 0, b'x', 0, 0, 0]); // frame_id, padded to 4
        expected.extend_from_slice(&[1, 0, 0, 0]); // one name
        expected.extend_from_slice(&[2, 0, 0, 0, b'a', 0, 0, 0]); // "a", padded to 4
        expected.extend_from_slice(&[1, 0, 0, 0]); // one position, already 8-aligned after it
        expected.extend_from_slice(&1.0_f64.to_le_bytes());
        expected.extend_from_slice(&[0, 0, 0, 0]); // no velocity
        expected.extend_from_slice(&[0, 0, 0, 0]); // no effort
        assert_eq!(bytes, expected);
    }
}
