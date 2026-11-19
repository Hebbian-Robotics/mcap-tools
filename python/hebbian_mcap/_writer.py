"""Grouped MCAP writer backed by the shared Rust implementation."""

import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import IO, NewType, Self

from ._native import DEFAULT_CHUNK_SIZE_BYTES, DEFAULT_LIBRARY_IDENTIFIER, NativeWriter

SchemaId = NewType("SchemaId", int)
ChannelId = NewType("ChannelId", int)
NO_SCHEMA_ID = SchemaId(0)


class CompressionType(StrEnum):
    ZSTD = "zstd"
    NONE = "none"


class _WriterState(StrEnum):
    ACTIVE = "active"
    FINALIZING = "finalizing"
    FINISHED = "finished"
    ABORTED = "aborted"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class _StagedOutput:
    destination: Path
    temporary_path: Path
    stream: IO[bytes]

    @classmethod
    def create(cls, destination: Path) -> Self:
        stream = tempfile.NamedTemporaryFile(  # noqa: SIM115 -- Owned until finish or abort.
            mode="w+b",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        )
        return cls(destination, Path(stream.name), stream)

    def publish(self) -> None:
        self.stream.close()
        self.temporary_path.replace(self.destination)

    def discard(self) -> None:
        try:
            self.stream.close()
        finally:
            self.temporary_path.unlink(missing_ok=True)


class GroupedMcapWriter:
    """MCAP writer with per-group chunk streams.

    Usage::

        with GroupedMcapWriter(path) as writer:
            schema_id = writer.register_schema(name, encoding, data)
            channel_id = writer.register_channel(
                topic, message_encoding, schema_id, group="cameras"
            )
            writer.write_message(channel_id, log_time, data, publish_time)
            writer.add_metadata("episode/v1", {...})
    """

    def __init__(
        self,
        output: Path | str | IO[bytes],
        *,
        chunk_size: int | Mapping[str, int] = DEFAULT_CHUNK_SIZE_BYTES,
        default_chunk_size: int = DEFAULT_CHUNK_SIZE_BYTES,
        compression: CompressionType | str = CompressionType.ZSTD,
        library: str | None = None,
    ) -> None:
        """Configure one chunk target for every group or targets by group name.

        Groups omitted from a mapping use ``default_chunk_size``.
        """
        chunk_size_by_group = {} if isinstance(chunk_size, int) else dict(chunk_size)
        for group_name, group_chunk_size in chunk_size_by_group.items():
            if group_chunk_size < 1:
                raise ValueError(
                    f"chunk size for group {group_name!r} must be >= 1, got {group_chunk_size}"
                )
        resolved_default_chunk_size = (
            chunk_size if isinstance(chunk_size, int) else default_chunk_size
        )
        if resolved_default_chunk_size < 1:
            parameter_name = "chunk_size" if isinstance(chunk_size, int) else "default_chunk_size"
            raise ValueError(f"{parameter_name} must be >= 1, got {resolved_default_chunk_size}")
        try:
            compression_type = CompressionType(compression)
        except ValueError:
            valid_compression_types = [
                supported_compression_type.value for supported_compression_type in CompressionType
            ]
            raise ValueError(
                f"unknown compression {compression!r}; expected one of {valid_compression_types}"
            ) from None

        self._staged_output: _StagedOutput | None = None
        if isinstance(output, str | Path):
            self._staged_output = _StagedOutput.create(Path(output).absolute())
            stream = self._staged_output.stream
        else:
            stream = output

        self._state = _WriterState.ACTIVE
        try:
            self._native = NativeWriter(
                stream,
                resolved_default_chunk_size,
                chunk_size_by_group,
                compression_type.value,
                library if library is not None else DEFAULT_LIBRARY_IDENTIFIER,
            )
        except BaseException:
            self._discard_temporary_output()
            raise

    def register_schema(self, name: str, encoding: str, data: bytes) -> SchemaId:
        self._raise_if_not_active("register a schema")
        return SchemaId(self._native.register_schema(name, encoding, data))

    def register_channel(
        self,
        topic: str,
        message_encoding: str,
        schema_id: SchemaId,
        *,
        group: str,
        metadata: dict[str, str] | None = None,
    ) -> ChannelId:
        self._raise_if_not_active("register a channel")
        return ChannelId(
            self._native.register_channel(
                topic,
                message_encoding,
                schema_id,
                group,
                metadata if metadata is not None else {},
            )
        )

    def write_message(
        self,
        channel_id: ChannelId,
        log_time: int,
        data: bytes,
        publish_time: int | None = None,
        sequence: int = 0,
    ) -> None:
        self._raise_if_not_active("write a message")
        self._native.write_message(
            channel_id,
            log_time,
            data,
            log_time if publish_time is None else publish_time,
            sequence,
        )

    def add_metadata(self, name: str, data: dict[str, str]) -> None:
        self._raise_if_not_active("add metadata")
        self._native.add_metadata(name, data)

    def add_attachment(
        self,
        name: str,
        media_type: str,
        data: bytes,
        *,
        log_time: int = 0,
        create_time: int = 0,
    ) -> None:
        self._raise_if_not_active("add an attachment")
        self._native.add_attachment(name, media_type, data, log_time, create_time)

    def finish(self) -> None:
        """Finalize and atomically publish path outputs. Successful finish is idempotent."""
        if self._state is _WriterState.FINISHED:
            return
        self._raise_if_not_active("finish")
        self._state = _WriterState.FINALIZING
        try:
            self._native.finish()
            if self._staged_output is not None:
                self._staged_output.publish()
        except BaseException:
            self._state = _WriterState.FAILED
            self._native.abort()
            self._discard_temporary_output()
            raise
        self._state = _WriterState.FINISHED

    def abort(self) -> None:
        """Abandon the write and delete only the unpublished temporary file.

        The destination is atomically replaced only by :meth:`finish`, so an
        aborted rewrite never destroys a previously published valid episode.
        Called automatically when an exception escapes the ``with`` block.
        """
        match self._state:
            case _WriterState.ACTIVE:
                self._state = _WriterState.ABORTED
                self._native.abort()
                self._discard_temporary_output()
            case _WriterState.FINALIZING:
                raise RuntimeError("cannot abort: writer is finalizing")
            case _WriterState.FINISHED | _WriterState.ABORTED | _WriterState.FAILED:
                return

    def _discard_temporary_output(self) -> None:
        if self._staged_output is not None:
            self._staged_output.discard()

    def _raise_if_not_active(self, operation: str) -> None:
        if self._state is not _WriterState.ACTIVE:
            raise RuntimeError(f"cannot {operation}: writer is {self._state.value}")

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.finish()
        else:
            self.abort()
