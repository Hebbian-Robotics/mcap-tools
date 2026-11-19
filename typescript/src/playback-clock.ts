// One recording clock that every clip follows. The clock, not any video, owns
// time: a clip only plays while the clock is inside it, and is nudged back
// whenever it drifts. The clock holds while an active clip is still loading.

export interface SyncedClip {
  readonly video: HTMLVideoElement;
  // Unix seconds of the clip's first frame.
  readonly startTime: number;
  readonly durationSeconds: number;
  /** Failed clips do not hold the recording clock. */
  readonly failed: boolean;
  // Shown while the clock is outside the clip.
  setIdle(idle: boolean): void;
  cacheLocally(): Promise<void>;
}

// Beyond this drift a playing clip seeks; below it, small rate changes would
// be invisible anyway.
const PLAYING_DRIFT_TOLERANCE_SECONDS = 0.2;
const PAUSED_DRIFT_TOLERANCE_SECONDS = 0.02;
const HAVE_FUTURE_DATA = 3;
// Above this rate browsers cannot decode every frame of every clip, so clips
// stop playing and skim instead: each jumps to the clock whenever its last
// jump has finished, and the clock never waits for them. The recorder's
// keyframe a second keeps those jumps cheap.
export const MAXIMUM_CONTINUOUS_PLAYBACK_RATE = 4;

export type ClockListener = (
  currentTime: number,
  playing: boolean,
  buffering: boolean,
) => void;

type PlaybackMode = "paused" | "buffering" | "playing" | "skimming";

export class RecordingClock {
  currentTime: number;
  playing = false;
  playbackRate = 1;
  private lastFrameAt: number | null = null;
  private frame: number | null;
  private readonly listeners = new Set<ClockListener>();

  constructor(
    readonly startTime: number,
    readonly endTime: number,
    private readonly clips: readonly SyncedClip[],
  ) {
    this.currentTime = startTime;
    this.frame = requestAnimationFrame((now) => this.advance(now));
  }

  onChange(listener: ClockListener): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  destroy(): void {
    if (this.frame === null) return;
    cancelAnimationFrame(this.frame);
    this.frame = null;
    this.playing = false;
    for (const clip of this.clips) if (!clip.video.paused) clip.video.pause();
    this.listeners.clear();
  }

  play(): void {
    if (this.frame === null) return;
    if (this.currentTime >= this.endTime) this.currentTime = this.startTime;
    this.playing = true;
  }

  pause(): void {
    this.playing = false;
  }

  togglePlaying(): void {
    if (this.playing) this.pause();
    else this.play();
  }

  seek(time: number): void {
    if (this.frame === null) return;
    this.currentTime = Math.min(this.endTime, Math.max(this.startTime, time));
    this.syncClips(this.playbackMode());
    this.notify();
  }

  private advance(now: number): void {
    if (this.frame === null) return;
    const elapsedSeconds =
      this.lastFrameAt === null ? 0 : (now - this.lastFrameAt) / 1000;
    this.lastFrameAt = now;
    const mode = this.playbackMode();
    if (mode === "playing" || mode === "skimming") {
      this.currentTime += elapsedSeconds * this.playbackRate;
      if (this.currentTime >= this.endTime) {
        this.currentTime = this.endTime;
        this.playing = false;
      }
    }
    this.syncClips(this.playbackMode());
    this.notify();
    if (this.frame !== null)
      this.frame = requestAnimationFrame((next) => this.advance(next));
  }

  get buffering(): boolean {
    return this.playbackMode() === "buffering";
  }

  private playbackMode(): PlaybackMode {
    if (!this.playing) return "paused";
    if (this.playbackRate > MAXIMUM_CONTINUOUS_PLAYBACK_RATE) return "skimming";
    return this.activeClipIsLoading() ? "buffering" : "playing";
  }

  private activeClipIsLoading(): boolean {
    return this.clips.some(
      (clip) =>
        !clip.failed &&
        this.clipOffset(clip) !== null &&
        (clip.video.seeking || clip.video.readyState < HAVE_FUTURE_DATA),
    );
  }

  // Seconds into the clip, or null while the clock is outside it.
  private clipOffset(clip: SyncedClip): number | null {
    const offset = this.currentTime - clip.startTime;
    return offset >= 0 && offset < clip.durationSeconds ? offset : null;
  }

  private syncClips(mode: PlaybackMode): void {
    for (const clip of this.clips) {
      const offset = this.clipOffset(clip);
      const { video } = clip;
      clip.setIdle(offset === null);
      if (this.frame === null) return;
      if (clip.failed || offset === null) {
        if (!video.paused) video.pause();
        continue;
      }
      if (mode === "skimming") {
        if (!video.paused) video.pause();
        if (
          !video.seeking &&
          Math.abs(video.currentTime - offset) > PAUSED_DRIFT_TOLERANCE_SECONDS
        )
          video.currentTime = offset;
        continue;
      }
      // Faster playback drifts further between frames before it shows.
      const tolerance =
        mode === "playing"
          ? PLAYING_DRIFT_TOLERANCE_SECONDS * Math.max(1, this.playbackRate)
          : PAUSED_DRIFT_TOLERANCE_SECONDS;
      if (!video.seeking && Math.abs(video.currentTime - offset) > tolerance)
        video.currentTime = offset;
      if (video.playbackRate !== this.playbackRate)
        video.playbackRate = this.playbackRate;
      // A play request can start a deferred load. Ready clips still hold
      // their positions until the clock can resume.
      const shouldPlay =
        mode === "playing" ||
        (mode === "buffering" && video.readyState < HAVE_FUTURE_DATA);
      if (shouldPlay && video.paused) void video.play().catch(() => undefined);
      else if (!shouldPlay && !video.paused) video.pause();
    }
  }

  private notify(): void {
    for (const listener of this.listeners)
      listener(this.currentTime, this.playing, this.buffering);
  }
}
