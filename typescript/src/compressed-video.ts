// Turns a camera's `foxglove.CompressedVideo` packets from a recording's MCAP
// into an MP4 the browser plays natively: the H.264 is repackaged, never
// re-encoded, and each packet keeps its capture time.

import {
  BufferTarget,
  EncodedPacket,
  EncodedVideoPacketSource,
  Mp4OutputFormat,
  Output,
} from "mediabunny";

export interface VideoPacket {
  /** Capture time in nanoseconds since the Unix epoch. */
  logTimeNanoseconds: bigint;
  /** One Annex B access unit, as carried in `foxglove.CompressedVideo`. */
  data: Uint8Array;
}

const NANOSECONDS_PER_SECOND = 1_000_000_000n;
const IDR_NAL_UNIT_TYPE = 5;
const SPS_NAL_UNIT_TYPE = 7;

/** The `data` field (number 3) of a `foxglove.CompressedVideo` message. */
export function compressedVideoData(message: Uint8Array): Uint8Array {
  let position = 0;
  function varint(): number {
    let value = 0;
    for (let shift = 0; ; shift += 7) {
      const byte = message[position++];
      if (byte === undefined) throw new Error("Truncated video message");
      value += (byte & 0x7f) * 2 ** shift;
      if ((byte & 0x80) === 0) return value;
    }
  }
  while (position < message.length) {
    const key = varint();
    const wireType = key & 7;
    if (wireType === 2) {
      const length = varint();
      if (key >> 3 === 3) return message.subarray(position, position + length);
      position += length;
    } else if (wireType === 0) {
      varint();
    } else if (wireType === 1) {
      position += 8;
    } else if (wireType === 5) {
      position += 4;
    } else {
      throw new Error("Unsupported video message encoding");
    }
  }
  throw new Error("Video message has no data");
}

function nalUnitTypes(accessUnit: Uint8Array): number[] {
  const types: number[] = [];
  for (let index = 0; index + 3 < accessUnit.length; index++) {
    if (
      accessUnit[index] === 0 &&
      accessUnit[index + 1] === 0 &&
      accessUnit[index + 2] === 1
    ) {
      types.push((accessUnit[index + 3] ?? 0) & 0x1f);
      index += 2;
    }
  }
  return types;
}

/** `avc1.PPCCLL` from the first SPS, which the browser's decoder needs to be told. */
function codecString(firstKeyFrame: Uint8Array): string {
  for (let index = 0; index + 6 < firstKeyFrame.length; index++) {
    if (
      firstKeyFrame[index] === 0 &&
      firstKeyFrame[index + 1] === 0 &&
      firstKeyFrame[index + 2] === 1 &&
      ((firstKeyFrame[index + 3] ?? 0) & 0x1f) === SPS_NAL_UNIT_TYPE
    ) {
      const hex = (byte: number | undefined) =>
        (byte ?? 0).toString(16).padStart(2, "0");
      return `avc1.${hex(firstKeyFrame[index + 4])}${hex(firstKeyFrame[index + 5])}${hex(firstKeyFrame[index + 6])}`;
    }
  }
  throw new Error("The first video packet has no SPS");
}

/**
 * Package packets in capture order into one MP4. The first packet must be a
 * key frame carrying SPS and PPS, as the recorder writes them.
 */
export async function remuxToMp4(
  packets: VideoPacket[],
  width: number,
  height: number,
): Promise<Uint8Array> {
  const first = packets[0];
  if (!first) throw new Error("No video packets to play");
  const start = first.logTimeNanoseconds;
  const output = new Output({
    format: new Mp4OutputFormat({ fastStart: "in-memory" }),
    target: new BufferTarget(),
  });
  const source = new EncodedVideoPacketSource("avc");
  output.addVideoTrack(source);
  await output.start();
  const seconds = (time: bigint) =>
    Number(time - start) / Number(NANOSECONDS_PER_SECOND);
  for (const [index, packet] of packets.entries()) {
    const next = packets[index + 1];
    const previous = packets[index - 1];
    const duration = next
      ? seconds(next.logTimeNanoseconds) - seconds(packet.logTimeNanoseconds)
      : previous
        ? seconds(packet.logTimeNanoseconds) -
          seconds(previous.logTimeNanoseconds)
        : 1 / 30;
    const keyFrame = nalUnitTypes(packet.data).includes(IDR_NAL_UNIT_TYPE);
    await source.add(
      new EncodedPacket(
        packet.data,
        keyFrame ? "key" : "delta",
        seconds(packet.logTimeNanoseconds),
        duration,
      ),
      index === 0
        ? {
            decoderConfig: {
              codec: codecString(first.data),
              codedWidth: width,
              codedHeight: height,
            },
          }
        : undefined,
    );
  }
  await output.finalize();
  const bytes = output.target.buffer;
  if (!bytes) throw new Error("The MP4 could not be written");
  return new Uint8Array(bytes);
}
