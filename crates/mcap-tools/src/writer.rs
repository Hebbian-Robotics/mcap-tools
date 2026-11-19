use mcap::{McapError, McapResult, Writer, records::MessageHeader};
use std::{
    collections::BTreeMap,
    io::{Seek, Write},
    num::NonZeroUsize,
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
    chunk_target: NonZeroUsize,
    chunk: Option<PendingChunk>,
}

struct PendingChunk {
    messages: Vec<PendingMessage>,
    bytes: usize,
    opened_at: Instant,
}

impl PendingChunk {
    fn is_due(&self, target: NonZeroUsize, maximum_age: Option<Duration>, now: Instant) -> bool {
        self.bytes > target.get()
            || maximum_age
                .is_some_and(|maximum_age| now.duration_since(self.opened_at) >= maximum_age)
    }
}

// Opcode, record length, channel ID, sequence, log time, and publish time.
const MESSAGE_RECORD_OVERHEAD: usize = 1 + 8 + 2 + 4 + 8 + 8;

enum WriterState<W: Write + Seek> {
    Active(Box<Writer<W>>),
    Finished,
    Failed,
}

/// One MCAP stream with independent buffers for named channel groups.
///
/// Upstream owns all file-format serialization. The wrapper never mixes messages
/// from different groups in a chunk. Dropping abandons buffered messages: explicitly
/// call `finish` for a complete archive or `flush` for durable partial data.
pub struct GroupedWriter<W: Write + Seek> {
    default_chunk_target: NonZeroUsize,
    chunk_targets_by_group: BTreeMap<String, NonZeroUsize>,
    max_chunk_age: Option<Duration>,
    groups: Vec<PendingGroup>,
    group_by_name: BTreeMap<String, usize>,
    group_by_channel: BTreeMap<u16, usize>,
    state: WriterState<W>,
}

fn invalid_input(message: impl Into<String>) -> McapError {
    std::io::Error::new(std::io::ErrorKind::InvalidInput, message.into()).into()
}

impl<W: Write + Seek> GroupedWriter<W> {
    pub fn new(output: W, options: WriterOptions) -> McapResult<Self> {
        let parse_chunk_target = |size| {
            NonZeroUsize::new(size).ok_or_else(|| invalid_input("chunk sizes must be positive"))
        };
        let default_chunk_target = parse_chunk_target(options.chunk_size)?;
        let chunk_targets_by_group = options
            .chunk_size_by_group
            .into_iter()
            .map(|(group, size)| Ok((group, parse_chunk_target(size)?)))
            .collect::<McapResult<_>>()?;
        let writer = mcap::WriteOptions::new()
            .compression(options.compression)
            .library(&options.library)
            // Group buffers, rather than upstream, decide chunk boundaries.
            .chunk_size(None)
            .disable_seeking(true)
            .create(output)?;
        Ok(Self {
            default_chunk_target,
            chunk_targets_by_group,
            max_chunk_age: options.max_chunk_age,
            groups: Vec::new(),
            group_by_name: BTreeMap::new(),
            group_by_channel: BTreeMap::new(),
            state: WriterState::Active(Box::new(writer)),
        })
    }

    fn active(&mut self) -> McapResult<&mut Writer<W>> {
        match &mut self.state {
            WriterState::Active(writer) => Ok(writer),
            WriterState::Finished | WriterState::Failed => {
                Err(McapError::AttemptedWriteAfterFailure)
            }
        }
    }

    fn abandon_writer(&mut self) {
        // Upstream's Drop auto-finishes. An aborted or failed archive must
        // never get a valid footer from an implicit finish.
        if let WriterState::Active(writer) = std::mem::replace(&mut self.state, WriterState::Failed)
        {
            let _ = writer.into_inner();
        }
        self.groups.clear();
    }

    fn write_to_archive<T>(
        &mut self,
        write: impl FnOnce(&mut Writer<W>) -> McapResult<T>,
    ) -> McapResult<T> {
        let result = write(self.active()?);
        // These validation errors are returned before upstream writes bytes.
        if let Err(error) = &result
            && !matches!(
                error,
                McapError::UnknownSchema(..) | McapError::TooManySchemas
            )
        {
            self.abandon_writer();
        }
        result
    }

    pub fn add_schema(&mut self, name: &str, encoding: &str, data: &[u8]) -> McapResult<u16> {
        self.write_to_archive(|writer| writer.add_schema(name, encoding, data))
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
        self.write_to_archive(|writer| {
            writer.add_channel_with_id(channel_id, schema_id, topic, encoding, metadata)
        })?;
        let group_index = if let Some(&index) = self.group_by_name.get(group) {
            index
        } else {
            let index = self.groups.len();
            self.groups.push(PendingGroup {
                chunk_target: self
                    .chunk_targets_by_group
                    .get(group)
                    .copied()
                    .unwrap_or(self.default_chunk_target),
                chunk: None,
            });
            self.group_by_name.insert(group.to_owned(), index);
            index
        };
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
        let now = Instant::now();
        let pending = self.groups[group_index]
            .chunk
            .get_or_insert_with(|| PendingChunk {
                messages: Vec::new(),
                bytes: 0,
                opened_at: now,
            });
        pending.bytes += data.len() + MESSAGE_RECORD_OVERHEAD;
        pending.messages.push(PendingMessage {
            header,
            data: data.to_vec(),
        });
        let candidate_groups = if self.max_chunk_age.is_some() {
            0..self.groups.len()
        } else {
            group_index..group_index + 1
        };
        for index in candidate_groups {
            let group = &self.groups[index];
            if group
                .chunk
                .as_ref()
                .is_some_and(|chunk| chunk.is_due(group.chunk_target, self.max_chunk_age, now))
            {
                self.flush_group(index)?;
            }
        }
        Ok(())
    }

    fn flush_group(&mut self, index: usize) -> McapResult<()> {
        let Some(chunk) = self.groups[index].chunk.take() else {
            return Ok(());
        };
        self.write_to_archive(|writer| {
            for message in chunk.messages {
                writer.write_to_known_channel(&message.header, &message.data)?;
            }
            writer.flush()
        })
    }

    pub fn write_metadata(&mut self, metadata: &mcap::records::Metadata) -> McapResult<()> {
        // Keep records after all messages submitted before them, and let pending
        // messages share the definitions chunk rather than emitting a metadata-only flush.
        self.flush()?;
        self.write_to_archive(|writer| writer.write_metadata(metadata))
    }

    pub fn attach(&mut self, attachment: &mcap::Attachment<'_>) -> McapResult<()> {
        self.flush()?;
        self.write_to_archive(|writer| writer.attach(attachment))
    }

    /// Flush waiting messages from every group, including groups below their target.
    pub fn flush(&mut self) -> McapResult<()> {
        self.active()?;
        for index in 0..self.groups.len() {
            self.flush_group(index)?;
        }
        self.write_to_archive(Writer::flush)
    }

    pub fn finish(&mut self) -> McapResult<()> {
        if matches!(self.state, WriterState::Finished) {
            return Ok(());
        }
        self.flush()?;
        let WriterState::Active(mut writer) =
            std::mem::replace(&mut self.state, WriterState::Failed)
        else {
            return Err(McapError::AttemptedWriteAfterFailure);
        };
        let finish_result = writer.finish();
        // Upstream finish flushes the footer. Release the stream in both outcomes.
        let _ = writer.into_inner();
        finish_result?;
        self.state = WriterState::Finished;
        Ok(())
    }
}

impl<W: Write + Seek> Drop for GroupedWriter<W> {
    fn drop(&mut self) {
        self.abandon_writer();
    }
}
