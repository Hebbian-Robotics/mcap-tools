use mcap::{McapError, McapResult, Writer, records::MessageHeader};
use std::{
    collections::BTreeMap,
    io::{Seek, Write},
    time::{Duration, Instant},
};

/// Buffering policy. Size counts serialized message bytes, including record headers.
#[derive(Clone)]
pub struct WriterOptions {
    pub chunk_size: usize,
    pub chunk_size_by_group: BTreeMap<String, usize>,
    /// Checked on each write. Call `flush` explicitly to persist idle groups.
    pub max_chunk_age: Option<Duration>,
    pub compression: Option<mcap::Compression>,
    pub library: String,
}

impl Default for WriterOptions {
    fn default() -> Self {
        Self {
            chunk_size: 1024 * 1024,
            chunk_size_by_group: BTreeMap::new(),
            max_chunk_age: None,
            compression: Some(mcap::Compression::Zstd),
            library: concat!("hebbian-mcap/", env!("CARGO_PKG_VERSION")).into(),
        }
    }
}

struct PendingMessage {
    header: MessageHeader,
    data: Vec<u8>,
}

struct PendingGroup {
    name: String,
    messages: Vec<PendingMessage>,
    bytes: usize,
    oldest: Option<Instant>,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum WriterState {
    Active,
    Finished,
    Failed,
}

/// One MCAP stream with independent buffers for named channel groups.
///
/// Upstream owns all file-format serialization. The wrapper never mixes messages
/// from different groups in a chunk. Dropping abandons buffered messages: explicitly
/// call `finish` for a complete archive or `flush` for durable partial data.
pub struct GroupedWriter<W: Write + Seek> {
    writer: Option<Writer<W>>,
    options: WriterOptions,
    groups: Vec<PendingGroup>,
    group_by_channel: BTreeMap<u16, usize>,
    state: WriterState,
}

fn invalid_input(message: impl Into<String>) -> McapError {
    std::io::Error::new(std::io::ErrorKind::InvalidInput, message.into()).into()
}

impl<W: Write + Seek> GroupedWriter<W> {
    pub fn new(output: W, options: WriterOptions) -> McapResult<Self> {
        if options.chunk_size == 0 || options.chunk_size_by_group.values().any(|size| *size == 0) {
            return Err(invalid_input("chunk sizes must be positive"));
        }
        let writer = mcap::WriteOptions::new()
            .compression(options.compression)
            .library(&options.library)
            // Group buffers, rather than upstream, decide chunk boundaries.
            .chunk_size(None)
            .disable_seeking(true)
            .create(output)?;
        Ok(Self {
            writer: Some(writer),
            options,
            groups: Vec::new(),
            group_by_channel: BTreeMap::new(),
            state: WriterState::Active,
        })
    }

    fn active(&mut self) -> McapResult<&mut Writer<W>> {
        if self.state != WriterState::Active {
            return Err(McapError::AttemptedWriteAfterFailure);
        }
        Ok(self.writer.as_mut().expect("writer exists until drop"))
    }

    pub fn add_schema(&mut self, name: &str, encoding: &str, data: &[u8]) -> McapResult<u16> {
        self.active()?.add_schema(name, encoding, data)
    }

    pub fn add_channel(
        &mut self,
        schema_id: u16,
        topic: &str,
        encoding: &str,
        metadata: &BTreeMap<String, String>,
        group: &str,
    ) -> McapResult<u16> {
        // Explicit IDs preserve distinct registrations even when their metadata
        // matches; identical channels may intentionally belong to different groups.
        let channel_id = u16::try_from(self.group_by_channel.len() + 1)
            .map_err(|_| McapError::TooManyChannels)?;
        self.active()?
            .add_channel_with_id(channel_id, schema_id, topic, encoding, metadata)?;
        let group_index = self
            .groups
            .iter()
            .position(|pending| pending.name == group)
            .unwrap_or_else(|| {
                self.groups.push(PendingGroup {
                    name: group.into(),
                    messages: Vec::new(),
                    bytes: 0,
                    oldest: None,
                });
                self.groups.len() - 1
            });
        self.group_by_channel.insert(channel_id, group_index);
        Ok(channel_id)
    }

    pub fn write_message(&mut self, header: MessageHeader, data: &[u8]) -> McapResult<()> {
        self.active()?;
        let group_index =
            *self
                .group_by_channel
                .get(&header.channel_id)
                .ok_or(McapError::UnknownChannel(
                    header.sequence,
                    header.channel_id,
                ))?;
        let pending = &mut self.groups[group_index];
        pending.oldest.get_or_insert_with(Instant::now);
        pending.bytes += data.len() + 31;
        pending.messages.push(PendingMessage {
            header,
            data: data.to_vec(),
        });
        for index in 0..self.groups.len() {
            let pending = &self.groups[index];
            let target = self
                .options
                .chunk_size_by_group
                .get(&pending.name)
                .copied()
                .unwrap_or(self.options.chunk_size);
            let due = pending.bytes > target
                || self.options.max_chunk_age.is_some_and(|maximum| {
                    pending
                        .oldest
                        .is_some_and(|oldest| oldest.elapsed() >= maximum)
                });
            if due {
                self.flush_group(index)?;
            }
        }
        Ok(())
    }

    fn flush_group(&mut self, index: usize) -> McapResult<()> {
        let messages = std::mem::take(&mut self.groups[index].messages);
        self.groups[index].bytes = 0;
        self.groups[index].oldest = None;
        if messages.is_empty() {
            return Ok(());
        }
        let result = (|| {
            let writer = self.active()?;
            for message in messages {
                writer.write_to_known_channel(&message.header, &message.data)?;
            }
            writer.flush()
        })();
        if result.is_err() {
            self.state = WriterState::Failed;
        }
        result
    }

    pub fn write_metadata(&mut self, metadata: &mcap::records::Metadata) -> McapResult<()> {
        // Keep records after all messages submitted before them, and let pending
        // messages share the definitions chunk rather than emitting a metadata-only flush.
        self.flush()?;
        self.active()?.write_metadata(metadata)
    }

    pub fn attach(&mut self, attachment: &mcap::Attachment<'_>) -> McapResult<()> {
        self.flush()?;
        self.active()?.attach(attachment)
    }

    /// Flush waiting messages from every group, including groups below their target.
    pub fn flush(&mut self) -> McapResult<()> {
        self.active()?;
        for index in 0..self.groups.len() {
            self.flush_group(index)?;
        }
        self.active()?.flush()
    }

    pub fn finish(&mut self) -> McapResult<()> {
        if self.state == WriterState::Finished {
            return Ok(());
        }
        let result = (|| {
            self.flush()?;
            self.active()?.finish()?;
            Ok(())
        })();
        if result.is_ok() {
            self.state = WriterState::Finished;
        } else {
            self.state = WriterState::Failed;
        }
        result
    }
}

impl<W: Write + Seek> Drop for GroupedWriter<W> {
    fn drop(&mut self) {
        // Upstream's Drop auto-finishes. Suppress it on abort/error: callers must
        // never mistake an incomplete write for a successful finalized recording.
        if let Some(writer) = self.writer.take() {
            let _ = writer.into_inner();
        }
    }
}
