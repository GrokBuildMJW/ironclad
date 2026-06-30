/**
 * flush — serializes diff Patches to a minimal ANSI byte stream (R2c).
 *
 * Applies the optimizer ideas (concept-spec §3) integrated into the emit pass: no-op
 * elimination (empty runs skipped), cursor-move minimization (vertical relative B/A,
 * horizontal absolute G, each elided when already in place), style-transition caching (via
 * the Palette transition cache), and wide-glyph handling (continuation cells skipped — the
 * lead advances two columns). The whole frame is wrapped in BSU/ESU for tearing-free atomic
 * display.
 *
 * Coordinates are relative to the live-region top-left. mount.ts (R7) owns origin/cursor-restore
 * against the scrollback.
 */
import type {Patch} from './diff.js';
import type {Palette} from './palette.js';
import {WIDE, WIDE_CONT} from './surface.js';

const CSI = '\x1b[';
export const BSU = CSI + '?2026h'; // begin synchronized update
export const ESU = CSI + '?2026l'; // end synchronized update

/** Wrap a frame body in BSU/ESU so the terminal displays it atomically (no tearing). */
export function withSync(body: string): string {
  return body ? BSU + body + ESU : '';
}

export interface RenderResult {
  body: string;
  row: number; // final cursor row (relative to live-region top)
  col: number; // final cursor column
  style: number; // final style id (0 = reset, restored at end)
}

/** Render a patch list to a relative-positioned ANSI body. */
export function renderPatches(patches: Patch[], palette: Palette): RenderResult {
  if (!patches.length) return {body: '', row: 0, col: 0, style: 0};
  const sorted = [...patches].sort((a, b) => a.y - b.y || a.x - b.x);
  let body = '';
  let curY = 0;
  let curX = 0;
  let style = 0;
  for (const p of sorted) {
    if (!p.cells.length) continue; // no-op elimination
    if (p.y !== curY) {
      body += p.y > curY ? `${CSI}${p.y - curY}B` : `${CSI}${curY - p.y}A`;
      curY = p.y;
    }
    if (p.x !== curX) {
      body += `${CSI}${p.x + 1}G`; // horizontal absolute (1-based column)
      curX = p.x;
    }
    for (const cell of p.cells) {
      if (cell.flag & WIDE_CONT) continue; // covered by the wide lead's 2-col advance
      if (cell.style !== style) {
        body += palette.transition(style, cell.style);
        style = cell.style;
      }
      body += String.fromCodePoint(cell.cp || 32);
      curX += cell.flag & WIDE ? 2 : 1;
    }
  }
  if (style !== 0) {
    body += palette.transition(style, 0); // leave the terminal in the default style
    style = 0;
  }
  return {body, row: curY, col: curX, style};
}
