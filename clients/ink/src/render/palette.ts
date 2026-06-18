/**
 * Palette — interns text styles to small integer ids and caches their ANSI sequences (R2).
 *
 * The Screen stores a style id per cell (not a style object). The pool maps a Style → id,
 * precomputes the SGR sequence for each id, and caches style→style transitions, so the
 * output stage never re-serializes a style: switching from "bold red" to "dim green" is one
 * cached string lookup. id 0 is reserved for the default (reset) style.
 */

export interface Style {
  fg?: string; // '#rrggbb' | named (red, blueBright, gray…) | 'ansi256:N'
  bg?: string;
  bold?: boolean;
  dim?: boolean;
  italic?: boolean;
  underline?: boolean;
  inverse?: boolean;
  strikethrough?: boolean;
}

const RESET = '\x1b[0m';

// named → ANSI 256-colour index (the 16 standard colours)
const NAMED: Record<string, number> = {
  black: 0, red: 1, green: 2, yellow: 3, blue: 4, magenta: 5, cyan: 6, white: 7,
  gray: 8, grey: 8, blackBright: 8,
  redBright: 9, greenBright: 10, yellowBright: 11, blueBright: 12,
  magentaBright: 13, cyanBright: 14, whiteBright: 15,
};

/** SGR colour parameters for fg (base 38) or bg (base 48). Returns '' for an unknown colour. */
export function colorParams(color: string, bg: boolean): string {
  const base = bg ? 48 : 38;
  if (color.startsWith('#') && color.length === 7) {
    const r = parseInt(color.slice(1, 3), 16);
    const g = parseInt(color.slice(3, 5), 16);
    const b = parseInt(color.slice(5, 7), 16);
    if (Number.isNaN(r) || Number.isNaN(g) || Number.isNaN(b)) return '';
    return `${base};2;${r};${g};${b}`;
  }
  if (color.startsWith('ansi256:')) {
    const n = parseInt(color.slice(8), 10);
    return Number.isFinite(n) ? `${base};5;${n}` : '';
  }
  const n = NAMED[color];
  return n === undefined ? '' : `${base};5;${n}`;
}

/** Full SGR sequence that applies `s` from a reset state (''. for the default style). */
export function styleSeq(s: Style): string {
  const p: string[] = [];
  if (s.bold) p.push('1');
  if (s.dim) p.push('2');
  if (s.italic) p.push('3');
  if (s.underline) p.push('4');
  if (s.inverse) p.push('7');
  if (s.strikethrough) p.push('9');
  if (s.fg) {
    const c = colorParams(s.fg, false);
    if (c) p.push(c);
  }
  if (s.bg) {
    const c = colorParams(s.bg, true);
    if (c) p.push(c);
  }
  return p.length ? `\x1b[${p.join(';')}m` : '';
}

function keyOf(s: Style): string {
  return [
    s.fg ?? '', s.bg ?? '',
    s.bold ? 1 : 0, s.dim ? 1 : 0, s.italic ? 1 : 0,
    s.underline ? 1 : 0, s.inverse ? 1 : 0, s.strikethrough ? 1 : 0,
  ].join('|');
}

export class Palette {
  private byKey = new Map<string, number>();
  private styles: Style[] = [{}]; // id 0 = default
  private seqs: string[] = ['']; // id 0 = no sequence
  private transitions = new Map<string, string>();

  /** Intern a style, returning its (stable, deduplicated) id. */
  intern(style: Style): number {
    const key = keyOf(style);
    let id = this.byKey.get(key);
    if (id === undefined) {
      id = this.styles.length;
      this.styles.push(style);
      this.seqs.push(styleSeq(style));
      this.byKey.set(key, id);
    }
    return id;
  }

  get(id: number): Style {
    return this.styles[id] ?? {};
  }

  /** SGR to apply style id from a reset state. */
  seq(id: number): string {
    return this.seqs[id] ?? '';
  }

  /**
   * Cached ANSI to switch from style `from` to style `to`. Reset-then-apply: correct in all
   * cases (no leftover attributes), and the cache makes the per-cell cost a map lookup.
   */
  transition(from: number, to: number): string {
    if (from === to) return '';
    const k = `${from},${to}`;
    let t = this.transitions.get(k);
    if (t === undefined) {
      t = to === 0 ? RESET : RESET + this.seq(to);
      this.transitions.set(k, t);
    }
    return t;
  }

  get size(): number {
    return this.styles.length;
  }
}
