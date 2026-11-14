import type { IReadable } from "@mcap/core";

async function fileSize(url: string): Promise<bigint> {
  const response = await fetch(url, { method: "HEAD", cache: "no-store" });
  const length = response.headers.get("content-length");
  if (!response.ok || length === null)
    throw new Error(`Could not size the remote file: ${response.status}`);
  return BigInt(length);
}

/** Read byte ranges of a remotely served file, so only needed chunks travel. */
export function httpRangeReadable(url: string): IReadable {
  let size: Promise<bigint> | undefined;
  return {
    size: () => {
      size ??= fileSize(url);
      return size;
    },
    async read(offset, length) {
      const response = await fetch(url, {
        headers: { Range: `bytes=${offset}-${offset + length - 1n}` },
        cache: "no-store",
      });
      if (!response.ok)
        throw new Error(`Could not read the remote file: ${response.status}`);
      const bytes = new Uint8Array(await response.arrayBuffer());
      // A server that ignores Range sends the whole file.
      return response.status === 206
        ? bytes
        : bytes.subarray(Number(offset), Number(offset + length));
    },
  };
}
