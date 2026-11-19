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

use super::cdr::CdrWriter;

/// One `JointState` stamped `timestamp_nanoseconds` after the Unix epoch, with
/// positions in radians. Velocities (radians per second) and efforts are each
/// either empty or one per joint.
pub fn encode(
    timestamp_nanoseconds: u64,
    frame_id: &str,
    names: &[String],
    positions: &[f64],
    velocities: &[f64],
    efforts: &[f64],
) -> std::io::Result<Vec<u8>> {
    let invalid_input = |message| std::io::Error::new(std::io::ErrorKind::InvalidInput, message);
    let seconds = i32::try_from(timestamp_nanoseconds / 1_000_000_000)
        .map_err(|_| invalid_input("JointState timestamp exceeds ROS 2 signed seconds range"))?;
    if names.len() != positions.len() {
        return Err(invalid_input(
            "JointState names and positions must have equal lengths",
        ));
    }
    if [velocities, efforts]
        .iter()
        .any(|values| !values.is_empty() && values.len() != names.len())
    {
        return Err(invalid_input(
            "JointState velocities and efforts must be empty or have one value per joint",
        ));
    }
    let cdr_error = |error| std::io::Error::new(std::io::ErrorKind::InvalidInput, error);
    let nanoseconds = u32::try_from(timestamp_nanoseconds % 1_000_000_000)
        .map_err(|_| invalid_input("JointState timestamp exceeds ROS 2 nanoseconds range"))?;
    let mut cdr = CdrWriter::new();
    cdr.write_i32(seconds);
    cdr.write_u32(nanoseconds);
    cdr.write_string(frame_id).map_err(cdr_error)?;
    cdr.write_length(names.len()).map_err(cdr_error)?;
    for name in names {
        cdr.write_string(name).map_err(cdr_error)?;
    }
    cdr.write_doubles(positions).map_err(cdr_error)?;
    cdr.write_doubles(velocities).map_err(cdr_error)?;
    cdr.write_doubles(efforts).map_err(cdr_error)?;
    Ok(cdr.into_bytes())
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    reason = "Test assertions fail immediately on unexpected outcomes"
)]
mod tests {
    use super::encode;

    #[test]
    fn layout_follows_cdr_alignment() {
        let bytes = encode(3_000_000_007, "x", &["a".to_owned()], &[1.0], &[], &[2.0]).unwrap();
        let mut expected = vec![0, 1, 0, 0]; // little-endian CDR
        expected.extend_from_slice(&3_i32.to_le_bytes()); // stamp.sec
        expected.extend_from_slice(&7_u32.to_le_bytes()); // stamp.nanosec
        expected.extend_from_slice(&[2, 0, 0, 0, b'x', 0, 0, 0]); // frame_id, padded to 4
        expected.extend_from_slice(&[1, 0, 0, 0]); // one name
        expected.extend_from_slice(&[2, 0, 0, 0, b'a', 0, 0, 0]); // "a", padded to 4
        expected.extend_from_slice(&[1, 0, 0, 0]); // one position, already 8-aligned after it
        expected.extend_from_slice(&1.0_f64.to_le_bytes());
        expected.extend_from_slice(&[0, 0, 0, 0]); // no velocity
        expected.extend_from_slice(&[1, 0, 0, 0]); // one effort, already 8-aligned after it
        expected.extend_from_slice(&2.0_f64.to_le_bytes());
        assert_eq!(bytes, expected);
    }
}
