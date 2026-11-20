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

test("destroy during setIdle stops the current frame and its successors", () => {
  const pending = new Map<number, FrameRequestCallback>();
  let frame = 0;
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    pending.set(++frame, callback);
    return frame;
  });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => pending.delete(id));
  const video = {
    seeking: false,
    readyState: 4,
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
  const play = vi.spyOn(video, "play");
  const laterIdle = vi.fn();
  const laterListener = vi.fn();
  const clip: SyncedClip = {
    video: video as HTMLVideoElement,
    startTime: 0,
    durationSeconds: 1,
    failed: false,
    setIdle: () => {},
    cacheLocally: async () => {},
  };
  const clock = new RecordingClock(0, 1, [
    clip,
    { ...clip, setIdle: laterIdle },
  ]);
  clip.setIdle = () => clock.destroy();
  clock.onChange(laterListener);
  clock.play();
  const tick = (now: number) => {
    const callbacks = [...pending.values()];
    pending.clear();
    for (const callback of callbacks) callback(now);
  };
  tick(0);
  expect(pending.size).toBe(0);
  expect(video.paused).toBe(true);
  expect(laterListener).not.toHaveBeenCalled();
  expect(play).not.toHaveBeenCalled();
  expect(laterIdle).not.toHaveBeenCalled();
  clock.destroy();
  expect(pending.size).toBe(0);
});
