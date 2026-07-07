/**
 * selection — app-owned text selection over the rendered Surface (R6, decision a).
 *
 * A drag sets an anchor and a moving focus; the selected region is the linear (reading-order) span
 * between them — partial on the first/last row, full rows in between, exactly like a terminal text
 * selection. The model exposes three things the renderer/clipboard need:
 *  - `isSelected(x,y)` — range membership, for drawing the inverse highlight.
 *  - `overlay(surface, palette)` — toggles the inverse attribute on selected cells (the visible
 *    highlight), skipping NoSelect chrome so frames/footer never look selected.
 *  - `extractText(surface, …)` — the copyable text: skips NoSelect cells and wide-glyph
 *    continuations, trims trailing whitespace per line, and (given a soft-wrap mask) re-joins a
 *    visually wrapped logical line into one line.
 */
import {Surface, WIDE, WIDE_CONT} from './surface.js';
import {Palette} from './palette.js';

export interface Cell {
  x: number;
  y: number;
}

export interface SelectionRange {
  start: Cell;
  end: Cell;
}

export interface ExtractOptions {
  /** Cells marked 1 are UI chrome → excluded from the copied text. */
  noSelect?: Uint8Array;
  /** Row y marked 1 is a soft-wrap continuation of y-1 → glued into one logical line. */
  softWrap?: Uint8Array;
}

export class Selection {
  private anchor: Cell | null = null;
  private focus: Cell | null = null;
  private dragging = false;

  /** Start a drag at (x,y). */
  begin(x: number, y: number): void {
    this.anchor = {x, y};
    this.focus = {x, y};
    this.dragging = true;
  }

  /** Move the focus end of the drag. */
  extend(x: number, y: number): void {
    if (this.anchor) this.focus = {x, y};
  }

  /** Finish the drag (the selection stays until cleared). */
  end(): void {
    this.dragging = false;
  }

  clear(): void {
    this.anchor = null;
    this.focus = null;
    this.dragging = false;
  }

  get isDragging(): boolean {
    return this.dragging;
  }

  get hasSelection(): boolean {
    const r = this.range;
    return r !== null && !(r.start.x === r.end.x && r.start.y === r.end.y);
  }

  /** Normalized range with start before end in reading order. */
  get range(): SelectionRange | null {
    if (!this.anchor || !this.focus) return null;
    const a = this.anchor;
    const b = this.focus;
    const aFirst = a.y < b.y || (a.y === b.y && a.x <= b.x);
    return aFirst ? {start: a, end: b} : {start: b, end: a};
  }

  /** Is the cell within the linear selection span? (Range only — NoSelect is applied by callers.) */
  isSelected(x: number, y: number): boolean {
    const r = this.range;
    if (!r) return false;
    const afterStart = y > r.start.y || (y === r.start.y && x >= r.start.x);
    const beforeEnd = y < r.end.y || (y === r.end.y && x <= r.end.x);
    return afterStart && beforeEnd;
  }

  /** Toggle the inverse attribute on selected cells (the visible highlight); skips NoSelect chrome. */
  overlay(surface: Surface, palette: Palette, noSelect?: Uint8Array, viewOffset = 0): void {
    const r = this.range;
    if (!r) return;
    const w = surface.width;
    for (let cy = r.start.y; cy <= r.end.y; cy++) {
      const y = cy - viewOffset; // #1173: the range is in CONTENT rows; map onto the screen surface
      if (y < 0 || y >= surface.height) continue;
      let xs = Math.max(0, cy === r.start.y ? r.start.x : 0);
      let xe = Math.min(w - 1, cy === r.end.y ? r.end.x : w - 1);
      // snap to whole wide glyphs: the lead carries the drawn glyph + its attributes
      if (xs > 0 && ((surface.flags[y * w + xs] ?? 0) & WIDE_CONT)) xs--;
      if (xe < w - 1 && ((surface.flags[y * w + xe] ?? 0) & WIDE)) xe++;
      for (let x = xs; x <= xe; x++) {
        const i = y * w + x;
        if (noSelect && noSelect[i]) continue;
        const cp = surface.code[i] ?? 32;
        const fl = surface.flags[i] ?? 0;
        const st = palette.get(surface.style[i] ?? 0);
        surface.setCell(x, y, cp, palette.intern({...st, inverse: !st.inverse}), fl);
      }
    }
  }

  /** The selected text, NoSelect-excluded, wide-glyph-safe, trailing-trimmed, soft-wrap-joined. */
  extractText(surface: Surface, opts: ExtractOptions = {}): string {
    const r = this.range;
    if (!r) return '';
    const {noSelect, softWrap} = opts;
    const w = surface.width;
    const lines: string[] = [];
    for (let y = r.start.y; y <= r.end.y; y++) {
      if (y < 0 || y >= surface.height) continue;
      let xs = Math.max(0, y === r.start.y ? r.start.x : 0);
      const xEnd = y === r.end.y ? r.end.x : w - 1;
      // snap left to a glyph lead so a selection that begins on a continuation still copies the glyph
      if (xs > 0 && ((surface.flags[y * w + xs] ?? 0) & WIDE_CONT)) xs--;
      let rowText = '';
      for (let x = xs; x <= xEnd && x < w; x++) {
        const i = y * w + x;
        if ((surface.flags[i] ?? 0) & WIDE_CONT) continue; // wide glyph: take the lead, skip its tail
        if (noSelect && noSelect[i]) continue; // chrome excluded from copy
        rowText += surface.getChar(x, y);
      }
      if (softWrap && softWrap[y] && lines.length > 0) {
        // continuation → glue; trim the previous row's padding FIRST so the join has no viewport gap
        lines[lines.length - 1] = (lines[lines.length - 1] ?? '').replace(/\s+$/, '') + rowText;
      } else {
        lines.push(rowText);
      }
    }
    return lines.map((l) => l.replace(/\s+$/, '')).join('\n');
  }
}
