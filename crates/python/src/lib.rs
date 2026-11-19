//! Thin Python transport adapter; all MCAP serialization lives in the Rust core.
use hebbian_mcap::{
    GroupedWriter, WriterOptions, mcap,
    messages::{self, MessageEncoding, MessageError},
};
use pyo3::{
    exceptions::{PyRuntimeError, PyValueError},
    prelude::*,
    types::{PyBytes, PyModule},
};
use std::{
    borrow::Cow,
    collections::BTreeMap,
    io::{self, Write},
    str::FromStr,
    sync::Mutex,
};

struct PythonOutput(Py<PyAny>);

impl Write for PythonOutput {
    fn write(&mut self, data: &[u8]) -> io::Result<usize> {
        let written = Python::attach(|python| {
            self.0
                .call_method1(python, "write", (PyBytes::new(python, data),))?
                .extract::<usize>(python)
        })
        .map_err(io::Error::from)?;
        if written > data.len() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "stream reported writing more bytes than it received",
            ));
        }
        Ok(written)
    }

    fn flush(&mut self) -> io::Result<()> {
        Python::attach(|python| self.0.call_method0(python, "flush").map(|_| ()))
            .map_err(io::Error::from)
    }
}

fn python_error(error: mcap::McapError) -> PyErr {
    match error {
        mcap::McapError::Io(error) => error.into(),
        mcap::McapError::UnknownChannel(_, channel_id) => {
            PyValueError::new_err(format!("unknown channel id {channel_id}"))
        }
        mcap::McapError::UnknownSchema(_, schema_id) => {
            PyValueError::new_err(format!("unknown schema id {schema_id}"))
        }
        mcap::McapError::AttemptedWriteAfterFailure => {
            PyRuntimeError::new_err("writer is failed or finished")
        }
        error => PyValueError::new_err(error.to_string()),
    }
}

#[pyclass(module = "hebbian_mcap._native")]
struct NativeWriter {
    writer: Mutex<Option<GroupedWriter<mcap::write::NoSeek<PythonOutput>>>>,
}

impl NativeWriter {
    fn active(&mut self) -> PyResult<&mut GroupedWriter<mcap::write::NoSeek<PythonOutput>>> {
        self.writer
            .get_mut()
            .map_err(|_| PyRuntimeError::new_err("writer lock poisoned"))?
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("writer is closed"))
    }
}

#[pymethods]
impl NativeWriter {
    #[new]
    fn new(
        output: Py<PyAny>,
        chunk_size: usize,
        chunk_size_by_group: BTreeMap<String, usize>,
        compression: &str,
        library: String,
    ) -> PyResult<Self> {
        let compression = match compression {
            "zstd" => Some(mcap::Compression::Zstd),
            "none" => None,
            _ => {
                return Err(PyValueError::new_err(format!(
                    "unknown compression {compression:?}"
                )));
            }
        };
        let options = WriterOptions {
            chunk_size,
            chunk_size_by_group,
            compression,
            library,
            ..WriterOptions::default()
        };
        Ok(Self {
            writer: Mutex::new(Some(
                GroupedWriter::new(mcap::write::NoSeek::new(PythonOutput(output)), options)
                    .map_err(python_error)?,
            )),
        })
    }

    fn register_schema(&mut self, name: &str, encoding: &str, data: &[u8]) -> PyResult<u16> {
        self.active()?
            .add_schema(name, encoding, data)
            .map_err(python_error)
    }

    // PyO3 extracts Python containers into owned Rust values at this boundary.
    #[allow(clippy::needless_pass_by_value)]
    fn register_channel(
        &mut self,
        topic: &str,
        message_encoding: &str,
        schema_id: u16,
        group: &str,
        metadata: BTreeMap<String, String>,
    ) -> PyResult<u16> {
        self.active()?
            .add_channel(schema_id, topic, message_encoding, &metadata, group)
            .map_err(python_error)
    }

    fn write_message(
        &mut self,
        channel_id: u16,
        log_time: u64,
        data: &[u8],
        publish_time: u64,
        sequence: u32,
    ) -> PyResult<()> {
        self.active()?
            .write_message(
                mcap::records::MessageHeader {
                    channel_id,
                    log_time,
                    publish_time,
                    sequence,
                },
                data,
            )
            .map_err(python_error)
    }

    fn add_metadata(&mut self, name: String, metadata: BTreeMap<String, String>) -> PyResult<()> {
        self.active()?
            .write_metadata(&mcap::records::Metadata { name, metadata })
            .map_err(python_error)
    }

    fn add_attachment(
        &mut self,
        name: String,
        media_type: String,
        data: &[u8],
        log_time: u64,
        create_time: u64,
    ) -> PyResult<()> {
        self.active()?
            .attach(&mcap::Attachment {
                name,
                media_type,
                data: Cow::Borrowed(data),
                log_time,
                create_time,
            })
            .map_err(python_error)
    }

    fn finish(&mut self) -> PyResult<()> {
        self.active()?.finish().map_err(python_error)
    }

    fn abort(&mut self) -> PyResult<()> {
        self.writer
            .get_mut()
            .map_err(|_| PyRuntimeError::new_err("writer lock poisoned"))?
            .take();
        Ok(())
    }
}

#[allow(clippy::needless_pass_by_value)]
fn message_error(error: MessageError) -> PyErr {
    PyValueError::new_err(error.to_string())
}

/// One decoded `CompressedVideo` message.
#[pyclass(frozen, get_all, module = "hebbian_mcap")]
struct CompressedVideo {
    /// `None` when the message carries no timestamp.
    timestamp_nanoseconds: Option<i64>,
    frame_id: String,
    data: Py<PyBytes>,
    format: String,
}

#[pyfunction]
#[pyo3(signature = (timestamp_nanoseconds, frame_id, data, format, message_encoding = "protobuf"))]
fn encode_compressed_video<'python>(
    python: Python<'python>,
    timestamp_nanoseconds: u64,
    frame_id: &str,
    data: &[u8],
    format: &str,
    message_encoding: &str,
) -> PyResult<Bound<'python, PyBytes>> {
    let encoding = MessageEncoding::from_str(message_encoding).map_err(message_error)?;
    let encoded = messages::CompressedVideo::stamped(timestamp_nanoseconds, frame_id, data, format)
        .encode_as(encoding)
        .map_err(message_error)?;
    Ok(PyBytes::new(python, &encoded))
}

/// Reads and rewrites the `CompressedVideo` messages of one MCAP channel.
#[pyclass(frozen, module = "hebbian_mcap")]
struct CompressedVideoCodec(messages::CompressedVideoCodec);

#[pymethods]
impl CompressedVideoCodec {
    #[new]
    fn new(
        message_encoding: &str,
        schema_encoding: &str,
        schema_name: &str,
        schema_data: &[u8],
    ) -> PyResult<Self> {
        messages::CompressedVideoCodec::for_channel(
            message_encoding,
            schema_encoding,
            schema_name,
            schema_data,
        )
        .map(Self)
        .map_err(message_error)
    }

    fn decode(&self, python: Python<'_>, message: &[u8]) -> PyResult<CompressedVideo> {
        let video = self.0.decode(message).map_err(message_error)?;
        let timestamp_nanoseconds = video
            .timestamp
            .map(|timestamp| {
                let nanoseconds =
                    i128::from(timestamp.seconds) * 1_000_000_000 + i128::from(timestamp.nanos);
                i64::try_from(nanoseconds).map_err(|_| {
                    PyValueError::new_err("CompressedVideo timestamp exceeds 64-bit nanoseconds")
                })
            })
            .transpose()?;
        Ok(CompressedVideo {
            timestamp_nanoseconds,
            frame_id: video.frame_id,
            data: PyBytes::new(python, &video.data).unbind(),
            format: video.format,
        })
    }

    fn replace_data<'python>(
        &self,
        python: Python<'python>,
        message: &[u8],
        data: &[u8],
    ) -> PyResult<Bound<'python, PyBytes>> {
        let replaced = self.0.replace_data(message, data).map_err(message_error)?;
        Ok(PyBytes::new(python, &replaced))
    }
}

#[pyfunction]
#[pyo3(signature = (timestamp_nanoseconds, frame_id, names, positions, velocities = Vec::new(), efforts = Vec::new()))]
#[allow(clippy::needless_pass_by_value)]
fn encode_joint_state<'python>(
    python: Python<'python>,
    timestamp_nanoseconds: u64,
    frame_id: &str,
    names: Vec<String>,
    positions: Vec<f64>,
    velocities: Vec<f64>,
    efforts: Vec<f64>,
) -> PyResult<Bound<'python, PyBytes>> {
    let data = hebbian_mcap::messages::joint_state::encode(
        timestamp_nanoseconds,
        frame_id,
        &names,
        &positions,
        &velocities,
        &efforts,
    )
    .map_err(|error| PyValueError::new_err(error.to_string()))?;
    Ok(PyBytes::new(python, &data))
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    let defaults = WriterOptions::default();
    module.add("DEFAULT_CHUNK_SIZE_BYTES", defaults.chunk_size)?;
    module.add("DEFAULT_LIBRARY_IDENTIFIER", defaults.library)?;
    module.add_class::<NativeWriter>()?;
    module.add_function(wrap_pyfunction!(encode_compressed_video, module)?)?;
    module.add_function(wrap_pyfunction!(encode_joint_state, module)?)?;
    module.add_class::<CompressedVideo>()?;
    module.add_class::<CompressedVideoCodec>()?;
    module.add("COMPRESSED_VIDEO_SCHEMA_NAME", messages::VIDEO_SCHEMA_NAME)?;
    module.add(
        "ROS2_COMPRESSED_VIDEO_SCHEMA_NAME",
        messages::ROS2_VIDEO_SCHEMA_NAME,
    )?;
    module.add(
        "ROS2_COMPRESSED_VIDEO_SCHEMA",
        PyBytes::new(module.py(), messages::ROS2_VIDEO_SCHEMA),
    )?;
    module.add(
        "COMPRESSED_VIDEO_SCHEMA",
        PyBytes::new(module.py(), hebbian_mcap::messages::VIDEO_SCHEMA),
    )?;
    module.add(
        "JOINT_STATE_SCHEMA",
        PyBytes::new(module.py(), hebbian_mcap::messages::joint_state::SCHEMA),
    )?;
    Ok(())
}
