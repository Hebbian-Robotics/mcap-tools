import { afterEach, expect, test, vi } from "vitest";
import { RecordingClock, type SyncedClip } from "../src/playback-clock.js";

afterEach(() => vi.unstubAllGlobals());

test("a failed active clip does not hold other cameras or the recording clock", () => {
  let nextFrame: FrameRequestCallback | undefined;
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    nextFrame = callback;
    return 1;
  });
  function clip(readyState: number): SyncedClip {
    const video = {
      seeking: false,
      readyState,
      paused: true,
      currentTime: 0,
      playbackRate: 1,
      play() {
        this.paused = false;
        return Promise.resolve();
      },
      pause() {
        this.paused = true;
      },
    };
    return {
      video: video as HTMLVideoElement,
      startTime: 0,
      durationSeconds: 10,
      failed: false,
      setIdle: () => {},
      cacheLocally: async () => {},
    };
  }
  const failedClip = clip(0);
  const workingClip = clip(4);
  const clock = new RecordingClock(0, 10, [failedClip, workingClip]);
  clock.play();
  nextFrame?.(0);
  expect(clock.buffering).toBe(true);
  expect(workingClip.video.paused).toBe(true);
  Object.defineProperty(failedClip, "failed", { value: true });
  nextFrame?.(1000);
  expect(clock.buffering).toBe(false);
  expect(clock.currentTime).toBe(1);
  expect(workingClip.video.currentTime).toBe(1);
  expect(workingClip.video.paused).toBe(false);
});

test("destroying the clock during notification stops the animation loop", () => {
  const callbacks: FrameRequestCallback[] = [];
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) =>
    callbacks.push(callback),
  );
  vi.stubGlobal("cancelAnimationFrame", () => {});
  const clock = new RecordingClock(0, 10, []);
  clock.onChange(() => clock.destroy());
  callbacks.shift()?.(0);
  expect(callbacks).toHaveLength(0);
});
