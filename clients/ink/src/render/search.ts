/**
 * search — find-in-buffer over the rendered Surface with n/N navigation (R6).
 *
 * Scans each row's visible text (wide glyphs counted once, NoSelect chrome optionally excluded),
 * locates every occurrence of the query, and maps each hit back to a **cell** range so the renderer
 * can highlight it. Tracks a current match for n/N (`next`/`previous`, wrapping), and `overlay`
 * paints all matches — the current one in a distinct style — so the user sees where they are.
 */
import {Surface, WIDE_CONT, charWidth} from './surface.js';
import {Palette, type Style} from './palette.js';

export interface Match {
  y: number;
  startX: number; // first cell column of the match
  endX: number; // last cell column (inclusive; accounts for a trailing wide glyph)
}

export interface FindOptions {
  caseSensitive?: boolean;
  /** Cells marked 1 are chrome → excluded from the searched text. */
  noSelect?: Uint8Array;
}

export interface HighlightStyles {
  match?: Style;
  current?: Style;
}

const DEFAULT_MATCH: Style = {bg: 'yellow', fg: 'black'};
const DEFAULT_CURRENT: Style = {bg: 'magenta', fg: 'white', bold: true};

export class Search {
  private matches: Match[] = [];
  private index = -1;

  /** Find all matches for `query`; resets the current match to the first. Empty query clears. */
  find(surface: Surface, query: string, opts: FindOptions = {}): Match[] {
    this.matches = query ? this.scan(surface, query, opts) : [];
    this.index = this.matches.length > 0 ? 0 : -1;
    return this.matches;
  }

  private scan(surface: Surface, query: string, opts: FindOptions): Match[] {
    const cs = opts.caseSensitive ?? false;
    const noSelect = opts.noSelect;
    const needle = cs ? query : query.toLowerCase();
    if (!needle) return [];
    const out: Match[] = [];
    for (let y = 0; y < surface.height; y++) {
      // Build the (optionally case-folded) haystack and map every CODE UNIT to its cell column +
      // glyph width. indexOf works in UTF-16 code units, so the maps must be code-unit-aligned —
      // an astral codepoint is 2 units / 1 cell, and a length-expanding lowercase fold (İ→i̇) would
      // otherwise desync every later offset.
      let hay = '';
      const cols: number[] = []; // code-unit index → cell column
      const widths: number[] = []; // code-unit index → glyph cell width
      for (let x = 0; x < surface.width; x++) {
        const i = y * surface.width + x;
        if ((surface.flags[i] ?? 0) & WIDE_CONT) continue;
        if (noSelect && noSelect[i]) continue;
        const ch = surface.getChar(x, y);
        const piece = cs ? ch : ch.toLowerCase();
        const w = charWidth(surface.code[i] ?? 32);
        hay += piece;
        for (let u = 0; u < piece.length; u++) {
          cols.push(x);
          widths.push(w);
        }
      }
      let from = 0;
      for (;;) {
        const idx = hay.indexOf(needle, from);
        if (idx < 0) break;
        const last = idx + needle.length - 1;
        const startX = cols[idx] ?? 0;
        const endX = (cols[last] ?? startX) + ((widths[last] ?? 1) - 1);
        out.push({y, startX, endX});
        from = idx + Math.max(1, needle.length);
      }
    }
    return out;
  }

  get count(): number {
    return this.matches.length;
  }

  get currentIndex(): number {
    return this.index;
  }

  get current(): Match | null {
    return this.index >= 0 ? (this.matches[this.index] ?? null) : null;
  }

  /** Advance to the next match (wraps); returns it for scroll-into-view. */
  next(): Match | null {
    if (this.matches.length === 0) return null;
    this.index = (this.index + 1) % this.matches.length;
    return this.current;
  }

  /** Step to the previous match (wraps). */
  previous(): Match | null {
    if (this.matches.length === 0) return null;
    this.index = (this.index - 1 + this.matches.length) % this.matches.length;
    return this.current;
  }

  clear(): void {
    this.matches = [];
    this.index = -1;
  }

  /** Whether a cell is in any match, and whether it's in the current one. */
  isMatch(x: number, y: number): {match: boolean; current: boolean} {
    for (let k = 0; k < this.matches.length; k++) {
      const m = this.matches[k];
      if (m && m.y === y && x >= m.startX && x <= m.endX) {
        return {match: true, current: k === this.index};
      }
    }
    return {match: false, current: false};
  }

  /**
   * Paint a highlight over every match (the current one distinct), merged onto each cell's style.
   * `yOffset` subtracts the scroll position so matches found in full-content coordinates land on the
   * right visible row (matches scrolled off screen are skipped).
   */
  overlay(surface: Surface, palette: Palette, styles: HighlightStyles = {}, yOffset = 0): void {
    const matchStyle = styles.match ?? DEFAULT_MATCH;
    const currentStyle = styles.current ?? DEFAULT_CURRENT;
    for (let k = 0; k < this.matches.length; k++) {
      const m = this.matches[k];
      if (!m) continue;
      const y = m.y - yOffset;
      if (y < 0 || y >= surface.height) continue;
      const hl = k === this.index ? currentStyle : matchStyle;
      for (let x = Math.max(0, m.startX); x <= m.endX && x < surface.width; x++) {
        const i = y * surface.width + x;
        const cp = surface.code[i] ?? 32;
        const fl = surface.flags[i] ?? 0;
        const base = palette.get(surface.style[i] ?? 0);
        surface.setCell(x, y, cp, palette.intern({...base, ...hl}), fl);
      }
    }
  }
}
