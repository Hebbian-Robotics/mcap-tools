from typing import IO, final

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
COMPRESSED_VIDEO_SCHEMA_NAME: str
ROS2_COMPRESSED_VIDEO_SCHEMA: bytes
ROS2_COMPRESSED_VIDEO_SCHEMA_NAME: str
JOINT_STATE_SCHEMA: bytes

@final
class CompressedVideo:
    @property
    def timestamp_nanoseconds(self) -> int | None: ...
    @property
    def frame_id(self) -> str: ...
    @property
    def data(self) -> bytes: ...
    @property
    def format(self) -> str: ...

@final
class CompressedVideoCodec:
    def __init__(
        self, message_encoding: str, schema_encoding: str, schema_name: str, schema_data: bytes
    ) -> None: ...
    def decode(self, message: bytes) -> CompressedVideo: ...
    def replace_data(self, message: bytes, data: bytes) -> bytes: ...

def encode_compressed_video(
    timestamp_nanoseconds: int,
    frame_id: str,
    data: bytes,
    format: str,
    message_encoding: str = "protobuf",
) -> bytes: ...
def encode_joint_state(
    timestamp_nanoseconds: int, frame_id: str, names: list[str], positions: list[float]
) -> bytes: ...
