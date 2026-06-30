/**
 * buffers — the double buffer the renderer composes frames in (R4, pattern §2).
 *
 * Two Surfaces of identical size: the **front** holds what is currently on screen (frame N-1),
 * the **back** is where the next frame (N) is composed (a full repaint of the tree). The output
 * stage diffs front→back, writes the minimal patch, then `swap()`s so the back becomes the new
 * front. A pointer swap (a boolean flip) means no array copying per frame. (A partial-repaint blit
 * path was prototyped but never wired and has been removed — #503 INK-R-2; correctness over the
 * micro-optimization, and the front→back cell diff already minimizes the terminal write.)
 *
 * Each frame also carries metadata (concept-spec §2): the viewport dimensions, so the diff stays
 * correct across a resize.
 */
import {Surface} from './surface.js';

export interface FrameMeta {
  /** Viewport width the frame was composed for. */
  width: number;
  /** Viewport height the frame was composed for. */
  height: number;
}

export class Buffers {
  width: number;
  height: number;
  /** Metadata of the back (in-progress) frame. */
  meta: FrameMeta;

  private a: Surface;
  private b: Surface;
  private backIsA = true; // which physical surface is currently the back buffer

  constructor(width: number, height: number) {
    this.width = Math.max(0, width | 0);
    this.height = Math.max(0, height | 0);
    this.a = new Surface(this.width, this.height);
    this.b = new Surface(this.width, this.height);
    this.meta = {width: this.width, height: this.height};
  }

  /** The surface the next frame is composed into (paint writes here). */
  get back(): Surface {
    return this.backIsA ? this.a : this.b;
  }

  /** The surface currently on screen — read for the front→back diff. */
  get front(): Surface {
    return this.backIsA ? this.b : this.a;
  }

  /** Promote the composed back frame to the front (pointer flip — no copy). */
  swap(): void {
    this.backIsA = !this.backIsA;
  }

  /**
   * Resize both surfaces (a resize is a conceptually new frame → both get full damage). The
   * front is resized too so a subsequent diff compares like-sized buffers (concept-spec §2:
   * stale viewport dims are the classic post-resize ghost source).
   */
  resize(width: number, height: number): void {
    this.width = Math.max(0, width | 0);
    this.height = Math.max(0, height | 0);
    this.a.resize(this.width, this.height);
    this.b.resize(this.width, this.height);
    this.meta.width = this.width;
    this.meta.height = this.height;
  }
}
