/**
 * Surface — the packed cell buffer the custom renderer draws into (R1).
 *
 * Cells are stored in three parallel typed arrays (no per-cell JS objects → no GC churn,
 * the property the analysis identifies as Stock Ink's killer): `code` (Unicode code point),
 * `style` (StylePool id), `flags` (wide-glyph bits). A damage rectangle tracks the bounding
 * box of cells written since the last `resetDamage()`, so the diff/output stages only touch
 * what changed.
 */

export const WIDE = 1; // flag bit: lead cell of a 2-column glyph (CJK / emoji)
export const WIDE_CONT = 2; // flag bit: trailing (continuation) cell of a 2-column glyph

/** Terminal display width of a code point: 0 (control/combining/zero-width), 1, or 2 (wide). */
export function charWidth(cp: number): 0 | 1 | 2 {
  if (cp === 0) return 0;
  if (cp < 32 || (cp >= 0x7f && cp < 0xa0)) return 0; // C0/C1 controls
  if (
    (cp >= 0x1100 && cp <= 0x115f) || // Hangul Jamo
    cp === 0x2329 ||
    cp === 0x232a ||
    (cp >= 0x2e80 && cp <= 0xa4cf && cp !== 0x303f) || // CJK Radicals … Yi
    (cp >= 0xac00 && cp <= 0xd7a3) || // Hangul Syllables
    (cp >= 0xf900 && cp <= 0xfaff) || // CJK Compatibility Ideographs
    (cp >= 0xfe30 && cp <= 0xfe4f) || // CJK Compatibility Forms
    (cp >= 0xff00 && cp <= 0xff60) || // Fullwidth Forms
    (cp >= 0xffe0 && cp <= 0xffe6) ||
    (cp >= 0x1f300 && cp <= 0x1faff) || // emoji & pictographs
    (cp >= 0x20000 && cp <= 0x3fffd) // CJK Ext-B and beyond
  ) {
    return 2;
  }
  return 1;
}

export interface DamageBox {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

export class Surface {
  width: number;
  height: number;
  code: Uint32Array;
  style: Uint16Array;
  flags: Uint8Array;
  private dmg: DamageBox | null = null;

  constructor(width: number, height: number) {
    this.width = Math.max(0, width | 0);
    this.height = Math.max(0, height | 0);
    const n = this.width * this.height;
    this.code = new Uint32Array(n);
    this.style = new Uint16Array(n);
    this.flags = new Uint8Array(n);
    this.clear();
  }

  private idx(x: number, y: number): number {
    return y * this.width + x;
  }

  inBounds(x: number, y: number): boolean {
    return x >= 0 && y >= 0 && x < this.width && y < this.height;
  }

  /** Fill blank (space, default style) and mark the whole screen damaged. */
  clear(): void {
    this.code.fill(32);
    this.style.fill(0);
    this.flags.fill(0);
    this.dmg =
      this.width && this.height ? {minX: 0, minY: 0, maxX: this.width - 1, maxY: this.height - 1} : null;
  }

  resetDamage(): void {
    this.dmg = null;
  }

  get damage(): DamageBox | null {
    return this.dmg;
  }

  private grow(x: number, y: number): void {
    if (!this.dmg) {
      this.dmg = {minX: x, minY: y, maxX: x, maxY: y};
      return;
    }
    if (x < this.dmg.minX) this.dmg.minX = x;
    if (y < this.dmg.minY) this.dmg.minY = y;
    if (x > this.dmg.maxX) this.dmg.maxX = x;
    if (y > this.dmg.maxY) this.dmg.maxY = y;
  }

  setCell(x: number, y: number, cp: number, style = 0, flag = 0): void {
    if (!this.inBounds(x, y)) return;
    const i = this.idx(x, y);
    this.code[i] = cp;
    this.style[i] = style;
    this.flags[i] = flag;
    this.grow(x, y);
  }

  getChar(x: number, y: number): string {
    if (!this.inBounds(x, y)) return '';
    return String.fromCodePoint(this.code[this.idx(x, y)] || 32);
  }

  getStyle(x: number, y: number): number {
    return this.inBounds(x, y) ? (this.style[this.idx(x, y)] ?? 0) : 0;
  }

  getFlag(x: number, y: number): number {
    return this.inBounds(x, y) ? (this.flags[this.idx(x, y)] ?? 0) : 0;
  }

  /**
   * Write `text` at (x,y), advancing by the display width of each glyph. A 2-wide glyph
   * occupies its lead cell (flag WIDE) and the next cell as a continuation (code 0, flag
   * WIDE_CONT) so the diff/output never splits it. Zero-width code points are skipped.
   * Returns the x just past the last written cell; clipped at the right edge.
   */
  setText(x: number, y: number, text: string, style = 0): number {
    for (const ch of text) {
      const cp = ch.codePointAt(0) ?? 32;
      const w = charWidth(cp);
      if (w === 0) continue;
      if (x >= this.width) break;
      if (w === 2) {
        this.setCell(x, y, cp, style, WIDE);
        if (x + 1 < this.width) this.setCell(x + 1, y, 0, style, WIDE_CONT);
        x += 2;
      } else {
        this.setCell(x, y, cp, style, 0);
        x += 1;
      }
    }
    return x;
  }

  /** Clear rows [y0, y1) to blank (space, default style) + mark them damaged — reserves an app-pinned
   *  region (e.g. the input + footer) so scrolled content painted there can be overwritten. */
  clearRows(y0: number, y1: number): void {
    const a = Math.max(0, y0 | 0);
    const b = Math.min(this.height, y1 | 0);
    for (let y = a; y < b; y++) {
      for (let x = 0; x < this.width; x++) {
        const i = this.idx(x, y);
        this.code[i] = 32;
        this.style[i] = 0;
        this.flags[i] = 0;
      }
      if (this.width > 0) {
        this.grow(0, y);
        this.grow(this.width - 1, y);
      }
    }
  }

  /** Reallocate to new dimensions (a resize is a conceptually new frame → full damage). */
  resize(width: number, height: number): void {
    const w = Math.max(0, width | 0);
    const h = Math.max(0, height | 0);
    if (w === this.width && h === this.height) {
      this.clear();
      return;
    }
    this.width = w;
    this.height = h;
    const n = w * h;
    this.code = new Uint32Array(n);
    this.style = new Uint16Array(n);
    this.flags = new Uint8Array(n);
    this.clear();
  }
}
