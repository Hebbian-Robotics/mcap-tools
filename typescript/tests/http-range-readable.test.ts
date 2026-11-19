import { once } from "node:events";
import { createServer } from "node:http";
import { expect, test } from "vitest";
import { httpRangeReadable } from "../src/http-range-readable.js";

test.each(["partial", "full", "short", "misaligned"] as const)(
  "reads or rejects a %s response to a byte range request",
  async (responseKind) => {
    const content = Buffer.from("recording bytes");
    const server = createServer((request, response) => {
      if (request.method === "HEAD") {
        response.writeHead(200, { "content-length": content.length }).end();
        return;
      }
      const range = request.headers.range?.match(/^bytes=(\d+)-(\d+)$/);
      if (responseKind !== "full" && range) {
        const start = Number(range[1]) + Number(responseKind === "misaligned");
        const end = Number(range[2]) + Number(responseKind === "misaligned");
        response
          .writeHead(206, {
            "content-range": `bytes ${start}-${end}/${content.length}`,
          })
          .end(content.subarray(start, end + Number(responseKind !== "short")));
      } else response.end(content);
    });
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    try {
      const address = server.address();
      if (!address || typeof address === "string")
        throw new Error("No listening port");
      const readable = httpRangeReadable(
        `http://127.0.0.1:${address.port}/recording.mcap`,
      );
      expect(await readable.size()).toBe(BigInt(content.length));
      if (responseKind === "short" || responseKind === "misaligned") {
        await expect(readable.read(3n, 6n)).rejects.toThrow("byte range");
      } else {
        expect(new TextDecoder().decode(await readable.read(3n, 6n))).toBe(
          "ording",
        );
      }
    } finally {
      server.closeAllConnections();
      await new Promise<void>((resolve, reject) =>
        server.close((error) => (error ? reject(error) : resolve())),
      );
    }
  },
);
