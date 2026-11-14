# Browser playback

Small TypeScript modules for browser applications reading recorded robot data.
No React, RUA, LiveKit or native extension is required.

Install from a pinned Git revision (the npm registry is not used):

```sh
pnpm add '@hebbian/mcap-tools@github:Hebbian-Robotics/mcap-tools#<commit>'
```

The package exports TypeScript source for bundlers that support it, such as
Vite. It is separate from the Python and Rust distributions in this repository.

```ts
import { McapIndexedReader } from "@mcap/core";
import { httpRangeReadable } from "@hebbian/mcap-tools/http-range-readable";

const reader = await McapIndexedReader.Initialize({
  readable: httpRangeReadable("/recordings/example.mcap"),
});
```

The server must support HEAD with Content-Length. Range responses avoid fetching
the whole recording; a server returning the complete file is also supported.
Cross-origin servers must allow the requests and expose Content-Length through
CORS. Supply decompression handlers to the MCAP reader for compressed files.

`compressedVideoData` extracts the data field from a
[Foxglove CompressedVideo](https://docs.foxglove.dev/docs/visualization/message-schemas/compressed-video)
protobuf message. `remuxToMp4(packets, width, height)` packages ordered Annex B
H.264 access units without re-encoding. The first packet must be a keyframe with
SPS/PPS; each packet carries its capture time as Unix nanoseconds. Split clips
when picture size changes. Remuxing uses [Mediabunny](https://mediabunny.dev/).

`RecordingClock` coordinates `SyncedClip` objects against one Unix-seconds
clock. It pauses time while active clips load, lets failed clips stop blocking
playback and seeks through clips at high playback rates. Call `destroy()` when
unmounting to cancel its animation loop. It uses browser video elements and
animation frames; applications own clip loading and rendering.

Topic interpretation, recording metadata, robot telemetry and UI belong to the
consumer. These modules do not define an application recording format. MCAP
reading uses the official [TypeScript library](https://mcap.dev/docs/typescript/).

Browser utilities and their tests are MIT licensed; see [LICENSE-MIT](../LICENSE-MIT).
