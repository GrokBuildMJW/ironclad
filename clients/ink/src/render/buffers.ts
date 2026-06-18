/**
 * buffers — the double buffer the renderer composes frames in (R4, pattern §2).
 *
 * Two Surfaces of identical size: the **front** holds what is currently on screen (frame N-1),
 * the **back** is where the next frame (N) is composed — dirty subtrees are painted into it and
 * unchanged ones are blitted from the front (blit.ts). The output stage diffs front→back, writes
 * the minimal patch, then `swap()`s so the back becomes the new front. A pointer swap (a boolean
 * flip) means no array copying per frame.
 *
 * Each frame also carries metadata (concept-spec §2): the viewport dimensions (so the diff stays
 * correct across a resize) and the cursor position/visibility (the main-screen cursor parks at the
 * content end for cheap relative moves; components may declare a position for IME/CJK pre-edit).
 */
import {Surface} from './surface.js';

export interface FrameMeta {
  /** Viewport width the frame was composed for. */
  width: number;
  /** Viewport height the frame was composed for. */
  height: number;
  /** Cursor column for the composed (back) frame. */
  cursorX: number;
  /** Cursor row for the composed (back) frame. */
  cursorY: number;
  /** Whether the hardware cursor should be shown for this frame. */
  cursorVisible: boolean;
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
    this.meta = {width: this.width, height: this.height, cursorX: 0, cursorY: 0, cursorVisible: false};
  }

  /** The surface the next frame is composed into (paint + blit write here). */
  get back(): Surface {
    return this.backIsA ? this.a : this.b;
  }

  /** The surface currently on screen — read for blit (copy unchanged cells) and the diff. */
  get front(): Surface {
    return this.backIsA ? this.b : this.a;
  }

  /** Promote the composed back frame to the front (pointer flip — no copy). */
  swap(): void {
    this.backIsA = !this.backIsA;
  }

  /** Blank the back buffer for a fresh full-frame compose (full damage). */
  clearBack(): void {
    this.back.clear();
  }

  /** Record the cursor for the frame being composed. */
  setCursor(x: number, y: number, visible: boolean): void {
    this.meta.cursorX = x | 0;
    this.meta.cursorY = y | 0;
    this.meta.cursorVisible = visible;
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
