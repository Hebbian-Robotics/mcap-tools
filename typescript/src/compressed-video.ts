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
const PPS_NAL_UNIT_TYPE = 8;
const VIDEO_DATA_FIELD = 3;

/** The `data` field (number 3) of a `foxglove.CompressedVideo` message. */
export function compressedVideoData(message: Uint8Array): Uint8Array {
  let position = 0;
  let data: Uint8Array | undefined;
  const openGroups: number[] = [];
  function readVarint(): number {
    let value = 0;
    for (let shift = 0; shift < 70; shift += 7) {
      const byte = message[position++];
      if (byte === undefined) throw new Error("Truncated video message");
      if (shift === 63 && byte > 1)
        throw new Error("Invalid video message varint");
      value += (byte & 0x7f) * 2 ** shift;
      if ((byte & 0x80) === 0) return value;
    }
    throw new Error("Invalid video message varint");
  }
  function skip(length: number): void {
    if (!Number.isSafeInteger(length) || length > message.length - position)
      throw new Error("Truncated video message");
    position += length;
  }
  while (position < message.length) {
    const key = readVarint();
    if (!Number.isSafeInteger(key) || key < 8 || key > 0xffffffff)
      throw new Error("Invalid video message field");
    const wireType = key & 7;
    const fieldNumber = key >>> 3;
    const isVideoData =
      openGroups.length === 0 && fieldNumber === VIDEO_DATA_FIELD;
    if (isVideoData && wireType !== 2)
      throw new Error("Video data field is not bytes");
    if (wireType === 2) {
      const length = readVarint();
      const start = position;
      skip(length);
      if (isVideoData) data = message.subarray(start, position);
    } else if (wireType === 0) {
      readVarint();
    } else if (wireType === 1) {
      skip(8);
    } else if (wireType === 3) {
      openGroups.push(fieldNumber);
    } else if (wireType === 4) {
      if (openGroups.pop() !== fieldNumber)
        throw new Error("Mismatched protobuf group in video message");
    } else if (wireType === 5) {
      skip(4);
    } else {
      throw new Error("Unsupported video message encoding");
    }
  }
  if (openGroups.length > 0) throw new Error("Truncated video message group");
  if (data === undefined) throw new Error("Video message has no data");
  return data;
}

function* annexBNalUnits(accessUnit: Uint8Array): Generator<Uint8Array> {
  let start: number | undefined;
  for (let index = 0; index + 3 < accessUnit.length; index++) {
    if (
      accessUnit[index] === 0 &&
      accessUnit[index + 1] === 0 &&
      accessUnit[index + 2] === 1
    ) {
      if (start !== undefined) {
        const end =
          index > start && accessUnit[index - 1] === 0 ? index - 1 : index;
        yield accessUnit.subarray(start, end);
      }
      start = index + 3;
      index += 2;
    }
  }
  if (start !== undefined) yield accessUnit.subarray(start);
}

function nalUnitType(unit: Uint8Array): number {
  return (unit[0] ?? 0) & 0x1f;
}

function packetType(data: Uint8Array): EncodedPacket["type"] {
  let foundNalUnit = false;
  for (const unit of annexBNalUnits(data)) {
    if (nalUnitType(unit) === IDR_NAL_UNIT_TYPE) return "key";
    foundNalUnit = true;
  }
  if (!foundNalUnit) throw new Error("Video packet has no Annex B NAL units");
  return "delta";
}

function firstPacketCodec(units: readonly Uint8Array[]): string {
  const sps = units.find((unit) => nalUnitType(unit) === SPS_NAL_UNIT_TYPE);
  if (
    !sps ||
    sps.length < 4 ||
    !units.some(
      (unit) => nalUnitType(unit) === PPS_NAL_UNIT_TYPE && unit.length > 1,
    ) ||
    !units.some((unit) => nalUnitType(unit) === IDR_NAL_UNIT_TYPE)
  ) {
    throw new Error(
      "The first video packet must be a key frame with SPS and PPS",
    );
  }
  // avc1.PPCCLL encodes the profile, compatibility flags and level from the SPS.
  const codecBytes = Array.from(sps.subarray(1, 4), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
  return `avc1.${codecBytes}`;
}

/**
 * Package packets in capture order into one MP4. The first packet must be a
 * key frame carrying SPS and PPS, as the recorder writes them.
 */
export async function remuxToMp4(
  packets: readonly VideoPacket[],
  width: number,
  height: number,
): Promise<Uint8Array> {
  const firstPacket = packets[0];
  if (!firstPacket) throw new Error("No video packets to play");
  if (
    !Number.isSafeInteger(width) ||
    width <= 0 ||
    !Number.isSafeInteger(height) ||
    height <= 0
  )
    throw new RangeError("Video dimensions must be positive integers");
  const firstPacketUnits = Array.from(annexBNalUnits(firstPacket.data));
  const decoderConfig = {
    codec: firstPacketCodec(firstPacketUnits),
    codedWidth: width,
    codedHeight: height,
  };
  const seconds = (nanoseconds: bigint) =>
    Number(nanoseconds) / Number(NANOSECONDS_PER_SECOND);
  const firstTimestamp = firstPacket.logTimeNanoseconds;
  const output = new Output({
    format: new Mp4OutputFormat({ fastStart: "in-memory" }),
    target: new BufferTarget(),
  });
  const source = new EncodedVideoPacketSource("avc");
  output.addVideoTrack(source);
  try {
    await output.start();
    for (const [index, packet] of packets.entries()) {
      const next = packets[index + 1];
      const previous = packets[index - 1];
      const interval = next
        ? next.logTimeNanoseconds - packet.logTimeNanoseconds
        : previous && packet.logTimeNanoseconds - previous.logTimeNanoseconds;
      if (interval !== undefined && interval <= 0n)
        throw new Error(
          "Video packet capture times must be strictly increasing",
        );
      const encodedPacket = new EncodedPacket(
        packet.data,
        index === 0 ? "key" : packetType(packet.data),
        seconds(packet.logTimeNanoseconds - firstTimestamp),
        interval === undefined ? 1 / 30 : seconds(interval),
      );
      await source.add(
        encodedPacket,
        index === 0 ? { decoderConfig } : undefined,
      );
    }
    await output.finalize();
  } catch (error) {
    await output.cancel().catch(() => undefined);
    throw error;
  }
  const bytes = output.target.buffer;
  if (!bytes) throw new Error("The MP4 could not be written");
  return new Uint8Array(bytes);
}
