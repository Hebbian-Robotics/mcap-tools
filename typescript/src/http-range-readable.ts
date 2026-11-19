import type { IReadable } from "@mcap/core";

async function fileSize(url: string): Promise<bigint> {
  const response = await fetch(url, { method: "HEAD", cache: "no-store" });
  const length = response.headers.get("content-length");
  if (!response.ok || length === null || !/^\d+$/.test(length))
    throw new Error(`Could not size the remote file: ${response.status}`);
  return BigInt(length);
}

/** Read byte ranges of a remotely served file, so only needed chunks travel. */
export function httpRangeReadable(url: string): IReadable {
  let size: Promise<bigint> | undefined;
  return {
    size: () => {
      size ??= fileSize(url).catch((error: unknown) => {
        size = undefined;
        throw error;
      });
      return size;
    },
    async read(offset, length) {
      if (offset < 0n || length < 0n)
        throw new RangeError(
          "A byte range cannot have a negative offset or length",
        );
      if (length === 0n) return new Uint8Array();
      const end = offset + length;
      const response = await fetch(url, {
        headers: { Range: `bytes=${offset}-${end - 1n}` },
        cache: "no-store",
      });
      if (response.status !== 200 && response.status !== 206)
        throw new Error(`Could not read the remote file: ${response.status}`);
      const bytes = new Uint8Array(await response.arrayBuffer());
      // A server that ignores Range sends the whole file.
      if (response.status === 200 && end <= BigInt(bytes.length))
        return bytes.subarray(Number(offset), Number(end));
      // Cross-origin responses can hide Content-Range from Fetch.
      const contentRange = response.headers.get("content-range");
      const range = contentRange?.match(/^bytes (\d+)-(\d+)\/(\d+|\*)$/i);
      const [, firstByte, lastByte, total] = range ?? [];
      const matchesRange =
        contentRange === null ||
        (firstByte !== undefined &&
          lastByte !== undefined &&
          total !== undefined &&
          BigInt(firstByte) === offset &&
          BigInt(lastByte) === end - 1n &&
          (total === "*" || BigInt(total) >= end));
      if (
        response.status === 206 &&
        BigInt(bytes.length) === length &&
        matchesRange
      )
        return bytes;
      throw new Error(
        "Remote file response did not contain the requested byte range",
      );
    },
  };
}
