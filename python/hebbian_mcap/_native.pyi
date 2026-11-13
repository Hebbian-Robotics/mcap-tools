from typing import IO

class NativeWriter:
    def __init__(
        self,
        output: IO[bytes],
        chunk_size: int,
        chunk_size_by_group: dict[str, int],
        compression: str,
        library: str,
    ) -> None: ...
    def register_schema(self, name: str, encoding: str, data: bytes) -> int: ...
    def register_channel(
        self,
        topic: str,
        message_encoding: str,
        schema_id: int,
        group: str,
        metadata: dict[str, str],
    ) -> int: ...
    def write_message(
        self, channel_id: int, log_time: int, data: bytes, publish_time: int, sequence: int
    ) -> None: ...
    def add_metadata(self, name: str, metadata: dict[str, str]) -> None: ...
    def add_attachment(
        self, name: str, media_type: str, data: bytes, log_time: int, create_time: int
    ) -> None: ...
    def finish(self) -> None: ...
    def abort(self) -> None: ...

COMPRESSED_VIDEO_SCHEMA: bytes
JOINT_STATE_SCHEMA: bytes

def encode_compressed_video(
    timestamp_nanoseconds: int, frame_id: str, data: bytes, format: str
) -> bytes: ...
def encode_joint_state(
    timestamp_nanoseconds: int, frame_id: str, names: list[str], positions: list[float]
) -> bytes: ...
