import { readFileSync } from "node:fs";
import { McapStreamReader } from "@mcap/core";
import { decompress } from "fzstd";
import {
  ALL_FORMATS,
  BufferSource,
  EncodedPacketSink,
  Input,
} from "mediabunny";
import { expect, test } from "vitest";
import {
  compressedVideoData,
  remuxToMp4,
  type VideoPacket,
} from "../src/compressed-video.js";

// A production MCAP writer captured these H.264 access units at irregular times.
// Ignore application metadata: the decoder only needs the standard video schema.
function videoPackets(): VideoPacket[] {
  const reader = new McapStreamReader({
    decompressHandlers: { zstd: (compressed) => decompress(compressed) },
  });
  reader.append(
    new Uint8Array(
      readFileSync(new URL("fixtures/with-video.mcap", import.meta.url)),
    ),
  );
  const videoSchemas = new Set<number>();
  const videoChannels = new Set<number>();
  const packets: VideoPacket[] = [];
  for (let record = reader.nextRecord(); record; record = reader.nextRecord()) {
    if (record.type === "Schema" && record.name === "foxglove.CompressedVideo")
      videoSchemas.add(record.id);
    if (record.type === "Channel" && videoSchemas.has(record.schemaId))
      videoChannels.add(record.id);
    if (record.type === "Message" && videoChannels.has(record.channelId))
      packets.push({
        logTimeNanoseconds: record.logTime,
        data: compressedVideoData(record.data),
      });
  }
  return packets.slice(0, 3);
}

test("remuxing preserves captured frame intervals and picture size", async () => {
  const mp4 = await remuxToMp4(videoPackets(), 16, 8);
  using input = new Input({
    source: new BufferSource(mp4),
    formats: ALL_FORMATS,
  });
  const track = await input.getPrimaryVideoTrack();
  if (!track) throw new Error("No video track");
  expect([track.codec, track.displayWidth, track.displayHeight]).toEqual([
    "avc",
    16,
    8,
  ]);
  const timestamps: number[] = [];
  for await (const packet of new EncodedPacketSink(track).packets())
    timestamps.push(packet.timestamp);
  expect(timestamps).toHaveLength(3);
  for (const [index, expected] of [0, 0.066667, 0.175111].entries())
    expect(timestamps[index]).toBeCloseTo(expected, 4);
});

test("an empty clip is refused instead of producing an unusable movie", async () => {
  await expect(remuxToMp4([], 16, 8)).rejects.toThrow("No video packets");
});
