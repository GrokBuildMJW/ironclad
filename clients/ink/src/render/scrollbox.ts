/**
 * scrollbox — app-managed scrollback over content taller than the viewport (R6, decision a).
 *
 * Owns the scroll position so the app (not the terminal's native scrollback, which the alt screen
 * disables) drives history: wheel, PageUp/Down, half-page (Ctrl+U/D) and vi keys (j/k/g/G). One
 * behaviour matters for a chat UI:
 *  - **Sticky bottom**: while parked at the end, new content auto-follows; scroll up and the view
 *    freezes at that position until you return to the bottom (which re-engages stick).
 */
import type {Key} from './hooks.js';

function clamp(v: number, lo: number, hi: number): number {
  return v < lo ? lo : v > hi ? hi : v;
}

export class ScrollBox {
  private contentHeight: number;
  private viewportHeight: number;
  private scrollTop = 0;
  private stick = true; // follow the bottom as content grows

  constructor(viewportHeight = 0, contentHeight = 0) {
    this.viewportHeight = Math.max(0, viewportHeight | 0);
    this.contentHeight = Math.max(0, contentHeight | 0);
    this.scrollTop = this.max;
  }

  /** Highest valid scrollTop (0 when content fits). */
  get max(): number {
    return Math.max(0, this.contentHeight - this.viewportHeight);
  }

  get top(): number {
    return this.scrollTop;
  }

  get atTop(): boolean {
    return this.scrollTop <= 0;
  }

  get atBottom(): boolean {
    return this.scrollTop >= this.max;
  }

  get stickToBottom(): boolean {
    return this.stick;
  }

  private reconcile(): void {
    this.scrollTop = this.stick ? this.max : clamp(this.scrollTop, 0, this.max);
  }

  setViewportHeight(h: number): void {
    this.viewportHeight = Math.max(0, h | 0);
    this.reconcile();
  }

  setContentHeight(h: number): void {
    this.contentHeight = Math.max(0, h | 0);
    this.reconcile();
  }

  /** Scroll to an absolute top; re-engages stick iff that lands at the bottom. */
  scrollTo(top: number): void {
    this.scrollTop = clamp(top, 0, this.max);
    this.stick = this.atBottom;
  }

  scrollBy(delta: number): void {
    this.scrollTo(this.scrollTop + delta);
  }

  lineUp(n = 1): void {
    this.scrollBy(-n);
  }

  lineDown(n = 1): void {
    this.scrollBy(n);
  }

  private get pageStep(): number {
    return Math.max(1, this.viewportHeight - 1); // keep one row of overlap
  }

  pageUp(): void {
    this.scrollBy(-this.pageStep);
  }

  pageDown(): void {
    this.scrollBy(this.pageStep);
  }

  toTop(): void {
    this.scrollTo(0);
  }

  toBottom(): void {
    this.scrollTo(this.max);
    this.stick = true;
  }

  /** Map a wheel event to a scroll; returns true (always consumed). */
  onWheel(action: 'wheelUp' | 'wheelDown', step = 3): boolean {
    if (action === 'wheelUp') this.lineUp(step);
    else this.lineDown(step);
    return true;
  }

  /** Map a scroll key (PageUp/Down, Ctrl+U/D half-page, vi j/k/g/G); returns true if consumed.
   *  A generic ScrollBox capability (tested in isolation). The chat client intentionally does NOT route
   *  keys through this — typed keys (incl. j/k/g/G) belong to the input box, and it wires PageUp/PageDown
   *  to scrollbox.pageUp()/pageDown() directly (mount.ts). Kept for any host that wants keyboard scrolling
   *  over a read-only pane (#503 INK-R-3: kept as a primitive, not wired here). */
  onKey(key: Key, input = ''): boolean {
    if (key.pageUp) {
      this.pageUp();
      return true;
    }
    if (key.pageDown) {
      this.pageDown();
      return true;
    }
    if (key.ctrl && input === 'u') {
      this.scrollBy(-Math.max(1, this.viewportHeight >> 1));
      return true;
    }
    if (key.ctrl && input === 'd') {
      this.scrollBy(Math.max(1, this.viewportHeight >> 1));
      return true;
    }
    if (!key.ctrl && !key.meta) {
      if (input === 'k') {
        this.lineUp();
        return true;
      }
      if (input === 'j') {
        this.lineDown();
        return true;
      }
      if (input === 'g') {
        this.toTop();
        return true;
      }
      if (input === 'G') {
        this.toBottom();
        return true;
      }
    }
    return false;
  }
}
