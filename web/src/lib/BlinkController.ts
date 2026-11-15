export const DEFAULT_FPS = 6;
export const MIN_FPS = 1;
export const MAX_FPS = 12;

export class BlinkController {
  readonly frameCount: number;
  frame = 0;
  playing = true;

  private currentFps = DEFAULT_FPS;
  private listener: ((frame: number) => void) | null = null;
  private handle: number | null = null;
  private lastTick = 0;
  private accumulator = 0;

  constructor(frameCount: number, fps = DEFAULT_FPS) {
    this.frameCount = Math.max(1, Math.floor(frameCount));
    this.setFps(fps);
  }

  get fps(): number {
    return this.currentFps;
  }

  setFps(value: number): void {
    this.currentFps = Math.min(
      MAX_FPS,
      Math.max(MIN_FPS, Math.round(value)),
    );
  }

  onFrame(listener: (frame: number) => void): void {
    this.listener = listener;
  }

  goTo(frame: number): void {
    const wrapped = ((Math.floor(frame) % this.frameCount) + this.frameCount) %
      this.frameCount;
    this.frame = wrapped;
    this.listener?.(this.frame);
  }

  advance(step = 1): number {
    this.goTo(this.frame + step);
    return this.frame;
  }

  toggle(): void {
    this.playing = !this.playing;
  }

  start(): void {
    if (this.handle !== null) return;
    this.lastTick = performance.now();
    this.accumulator = 0;
    const tick = (now: number) => {
      const elapsed = now - this.lastTick;
      this.lastTick = now;
      if (this.playing) {
        this.accumulator += elapsed;
        const interval = 1000 / this.currentFps;
        while (this.accumulator >= interval) {
          this.accumulator -= interval;
          this.advance(1);
        }
      } else {
        this.accumulator = 0;
      }
      this.handle = requestAnimationFrame(tick);
    };
    this.handle = requestAnimationFrame(tick);
  }

  stop(): void {
    if (this.handle !== null) {
      cancelAnimationFrame(this.handle);
      this.handle = null;
    }
  }

  destroy(): void {
    this.stop();
    this.listener = null;
  }
}
