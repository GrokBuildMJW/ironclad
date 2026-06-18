/**
 * paint — walk the laid-out vnode tree and draw it into a Surface (R3d).
 *
 * Precondition: `layout.attachYoga` + `layout.calculate` have run, so every element's
 * `vnode.yoga` carries computed geometry. paint reads that geometry, fills box backgrounds,
 * strokes borders (per `borderStyle`), and writes wrapped text with the node's interned
 * Palette style. It also produces a parallel **NoSelect** mask (1 byte/cell): borders are
 * always chrome (excluded from copy), and any subtree under `selectable={false}` is too —
 * so R6's selection/copy can keep UI chrome (frames, footer) out of the clipboard.
 *
 * This is a full-frame draw: it `clear()`s the surface first. Double-buffering and the
 * incremental diff live in R4/mount; here we just produce one correct frame + its mask.
 *
 * Geometry note: Yoga's getComputedLeft/Top are relative to the parent's border-box origin,
 * so a child's absolute position is the parent's absolute origin + the child's computed
 * left/top — we thread that origin down the walk (no per-node accumulation of padding/border,
 * Yoga already folded it in).
 */
import {Surface, charWidth} from './surface.js';
import {Palette, type Style} from './palette.js';
import type {VNode} from './vnode.js';

/** Minimal geometry view of a Yoga node — keeps paint decoupled from the WASM type + testable. */
interface YogaGeom {
  getComputedLeft(): number;
  getComputedTop(): number;
  getComputedWidth(): number;
  getComputedHeight(): number;
}

interface BorderSet {
  tl: string; t: string; tr: string;
  l: string; r: string;
  bl: string; b: string; br: string;
}

const SINGLE: BorderSet = {tl: '┌', t: '─', tr: '┐', l: '│', r: '│', bl: '└', b: '─', br: '┘'};

/** Box-drawing sets per Ink `borderStyle`. These are normal BMP glyphs — safe as literals. */
export const BORDERS: Record<string, BorderSet> = {
  single: SINGLE,
  round: {tl: '╭', t: '─', tr: '╮', l: '│', r: '│', bl: '╰', b: '─', br: '╯'},
  double: {tl: '╔', t: '═', tr: '╗', l: '║', r: '║', bl: '╚', b: '═', br: '╝'},
  bold: {tl: '┏', t: '━', tr: '┓', l: '┃', r: '┃', bl: '┗', b: '━', br: '┛'},
  classic: {tl: '+', t: '-', tr: '+', l: '|', r: '|', bl: '+', b: '-', br: '+'},
};

const cp = (ch: string): number => ch.codePointAt(0) ?? 32;

/** Build the text Style from an ink-text node's props (may be empty → no style). */
function textStyle(p: Record<string, unknown>): Style {
  const s: Style = {};
  if (typeof p['color'] === 'string') s.fg = p['color'];
  if (typeof p['backgroundColor'] === 'string') s.bg = p['backgroundColor'];
  if (p['bold']) s.bold = true;
  if (p['dimColor'] || p['dim']) s.dim = true; // Ink uses `dimColor` on <Text>
  if (p['italic']) s.italic = true;
  if (p['underline']) s.underline = true;
  if (p['inverse']) s.inverse = true;
  if (p['strikethrough']) s.strikethrough = true;
  return s;
}

function emptyStyle(s: Style): boolean {
  return !s.fg && !s.bg && !s.bold && !s.dim && !s.italic && !s.underline && !s.inverse && !s.strikethrough;
}

function markNoSelect(noSelect: Uint8Array, surface: Surface, x: number, y: number): void {
  if (surface.inBounds(x, y)) noSelect[y * surface.width + x] = 1;
}

/** A character carrying its resolved (interned) style id — the unit paint wraps + writes. */
interface SChar {
  ch: string;
  id: number;
}

/** Filled by paint with the hardware-cursor cell (screen coords) declared by a `cursor`-flagged text. */
export interface CursorOut {
  x: number;
  y: number;
  set: boolean;
}

/** Apply one SGR escape's parameters to a running style (set = assign, reset/default = delete). */
function applySgr(s: Style, params: string): void {
  const codes = params === '' ? [0] : params.split(';').map((n) => parseInt(n, 10) || 0);
  for (let i = 0; i < codes.length; i++) {
    const c = codes[i] ?? 0;
    if (c === 0) {
      delete s.fg; delete s.bg; delete s.bold; delete s.dim;
      delete s.italic; delete s.underline; delete s.inverse; delete s.strikethrough;
    } else if (c === 1) s.bold = true;
    else if (c === 2) s.dim = true;
    else if (c === 3) s.italic = true;
    else if (c === 4) s.underline = true;
    else if (c === 7) s.inverse = true;
    else if (c === 9) s.strikethrough = true;
    else if (c === 22) { delete s.bold; delete s.dim; }
    else if (c === 23) delete s.italic;
    else if (c === 24) delete s.underline;
    else if (c === 27) delete s.inverse;
    else if (c === 29) delete s.strikethrough;
    else if (c >= 30 && c <= 37) s.fg = 'ansi256:' + (c - 30);
    else if (c >= 90 && c <= 97) s.fg = 'ansi256:' + (c - 90 + 8);
    else if (c === 39) delete s.fg;
    else if (c >= 40 && c <= 47) s.bg = 'ansi256:' + (c - 40);
    else if (c >= 100 && c <= 107) s.bg = 'ansi256:' + (c - 100 + 8);
    else if (c === 49) delete s.bg;
    else if (c === 38 || c === 48) {
      const key = c === 38 ? 'fg' : 'bg';
      if (codes[i + 1] === 5) {
        s[key] = 'ansi256:' + (codes[i + 2] ?? 0);
        i += 2;
      } else if (codes[i + 1] === 2) {
        const r = codes[i + 2] ?? 0, g = codes[i + 3] ?? 0, b = codes[i + 4] ?? 0;
        s[key] = '#' + [r, g, b].map((v) => v.toString(16).padStart(2, '0')).join('');
        i += 4;
      }
    }
  }
}

const SGR_RE = /^\x1b\[([0-9;]*)m/;
const CSI_RE = /^\x1b\[[0-9;?]*[ -/]*[@-~]/;

/**
 * Push a text value as styled characters, parsing any embedded ANSI **SGR** escapes (the colors
 * marked-terminal/chalk bake into markdown) into the running style — so `\x1b[31m…\x1b[0m` colors
 * the text instead of leaking `[0m` as literal characters. Other (non-SGR) CSI sequences in content
 * are dropped. The SGR layer sits on top of the node's base style.
 */
function pushStyledText(value: string, base: Style, palette: Palette, out: SChar[]): void {
  const sgr: Style = {};
  let i = 0;
  while (i < value.length) {
    if (value.charCodeAt(i) === 0x1b && value[i + 1] === '[') {
      const rest = value.slice(i);
      const m = SGR_RE.exec(rest);
      if (m) {
        applySgr(sgr, m[1] ?? '');
        i += m[0].length;
        continue;
      }
      const csi = CSI_RE.exec(rest);
      if (csi) {
        i += csi[0].length;
        continue;
      }
    }
    const point = value.codePointAt(i) ?? 32;
    const ch = String.fromCodePoint(point);
    const merged: Style = {...base, ...sgr};
    out.push({ch, id: emptyStyle(merged) ? 0 : palette.intern(merged)});
    i += ch.length;
  }
}

/**
 * Flatten a (possibly nested) ink-text node into styled characters. A nested `<Text>` inherits its
 * parent Text's style and overrides only the props it sets (Ink-faithful); any cell without its own
 * background composites onto the nearest ancestor box background; and ANSI SGR escapes inside the
 * text content are parsed into per-character styles.
 */
function collectStyledChars(node: VNode, inherited: Style, bg: string | undefined, palette: Palette, out: SChar[]): void {
  const merged: Style = {...inherited, ...textStyle(node.props)};
  if (!merged.bg && bg) merged.bg = bg;
  for (const c of node.children) {
    if (c.kind === 'text') {
      pushStyledText(c.value, merged, palette, out);
    } else if (c.type === 'ink-text') {
      collectStyledChars(c, merged, bg, palette, out);
    }
  }
}

const isSpace = (ch: string): boolean => ch === ' ' || ch === '\t';
const cw = (c: SChar): number => charWidth(c.ch.codePointAt(0) ?? 32);

/**
 * Word-wrap styled characters to `width`, mirroring layout.wrapText exactly (so the painted line
 * count + widths match what the Yoga measure func computed): wrap at spaces, char-break an
 * over-long word, honor '\n', trim trailing whitespace per line. width ≤ 0 / Infinity → split on
 * '\n' only.
 */
function wrapStyled(chars: SChar[], width: number): {lines: SChar[][]; wrap: boolean[]} {
  const paras: SChar[][] = [[]];
  for (const c of chars) {
    if (c.ch === '\n') paras.push([]);
    else (paras[paras.length - 1] as SChar[]).push(c);
  }
  const lines: SChar[][] = [];
  const wrap: boolean[] = []; // wrap[i] = line i is a soft-wrap continuation (not a paragraph start)
  if (!Number.isFinite(width) || width <= 0) {
    for (const p of paras) {
      lines.push(p);
      wrap.push(false);
    }
    return {lines, wrap};
  }

  for (const para of paras) {
    const start = lines.length;
    let line: SChar[] = [];
    let lineW = 0;
    const push = (l: SChar[]): void => {
      wrap.push(lines.length > start); // the first line of a paragraph is a hard break, not a soft wrap
      lines.push(l);
    };
    const flush = (): void => {
      let e = line.length;
      while (e > 0 && isSpace((line[e - 1] as SChar).ch)) e--; // wrap point → drop the dangling space
      push(line.slice(0, e));
      line = [];
      lineW = 0;
    };
    // tokenize into alternating whitespace / non-whitespace runs (keeps inter-word spacing)
    const toks: SChar[][] = [];
    for (const c of para) {
      const last = toks[toks.length - 1];
      if (last && isSpace((last[0] as SChar).ch) === isSpace(c.ch)) last.push(c);
      else toks.push([c]);
    }
    for (const tok of toks) {
      const tw = tok.reduce((a, c) => a + cw(c), 0);
      if (lineW + tw <= width) {
        line.push(...tok);
        lineW += tw;
      } else if (isSpace((tok[0] as SChar).ch)) {
        if (line.length) flush(); // overflowing whitespace → break, drop the space
      } else if (tw > width) {
        for (const c of tok) {
          const w2 = cw(c);
          if (lineW + w2 > width && line.length) flush();
          line.push(c);
          lineW += w2;
        }
      } else {
        if (line.length) flush();
        line = [...tok];
        lineW = tw;
      }
    }
    // final line keeps its trailing whitespace (matches layout.wrapText so widths agree); emit an
    // empty final line only for a genuinely blank paragraph.
    if (line.length > 0 || lines.length === start) push(line);
  }
  return {lines, wrap};
}

function paintText(
  node: VNode, x: number, y: number, w: number, h: number,
  surface: Surface, palette: Palette, noSelect: Uint8Array, noSel: boolean,
  bg: string | undefined, softWrap: Uint8Array | undefined, cursor: CursorOut | undefined,
): void {
  const chars: SChar[] = [];
  collectStyledChars(node, {}, bg, palette, chars);
  if (chars.length === 0) return;
  const {lines, wrap} = wrapStyled(chars, w > 0 ? w : Infinity);
  const maxLines = h > 0 ? h : lines.length; // clip to the measured box height
  let endX = x;
  let endRow = y;
  for (let li = 0; li < lines.length && li < maxLines; li++) {
    const line = lines[li] ?? [];
    const row = y + li;
    if (softWrap && wrap[li] && row >= 0 && row < surface.height) softWrap[row] = 1;
    let xx = x;
    let runText = '';
    let runId = 0;
    let open = false;
    const flushRun = (): void => {
      if (!runText) return;
      const end = surface.setText(xx, y + li, runText, runId);
      if (noSel) for (let k = xx; k < end; k++) markNoSelect(noSelect, surface, k, y + li);
      xx = end;
      runText = '';
    };
    for (const c of line) {
      if (!open || c.id !== runId) {
        flushRun();
        runId = c.id;
        open = true;
      }
      runText += c.ch;
    }
    flushRun();
    endX = xx;
    endRow = row;
  }
  // a `cursor`-flagged text declares the hardware cursor just after its last character (the caret)
  if (cursor && !cursor.set && node.props['cursor'] && endRow >= 0 && endRow < surface.height) {
    cursor.x = Math.min(endX, surface.width - 1);
    cursor.y = endRow;
    cursor.set = true;
  }
}

function drawBorder(
  node: VNode, x: number, y: number, w: number, h: number,
  surface: Surface, palette: Palette, noSelect: Uint8Array,
): void {
  if (w < 2 || h < 2) return; // no room even for the two corners on an edge
  const p = node.props;
  const set = BORDERS[String(p['borderStyle'])] ?? SINGLE;
  const bc = p['borderColor'];
  const dim = p['borderDimColor'] === true;
  let id = 0;
  if (typeof bc === 'string' || dim) {
    const bs: Style = {};
    if (typeof bc === 'string') bs.fg = bc;
    if (dim) bs.dim = true;
    id = palette.intern(bs);
  }
  const x2 = x + w - 1;
  const y2 = y + h - 1;
  const put = (px: number, py: number, ch: string): void => {
    if (!surface.inBounds(px, py)) return;
    surface.setCell(px, py, cp(ch), id, 0);
    noSelect[py * surface.width + px] = 1; // borders are chrome, never copied
  };
  const top = p['borderTop'] !== false;
  const bottom = p['borderBottom'] !== false;
  const left = p['borderLeft'] !== false;
  const right = p['borderRight'] !== false;
  // A corner is drawn only where two edges actually meet; an open side continues the present edge
  // instead — so a top/bottom-only box renders as clean horizontal rules (no stray ┌┐└┘ corners).
  if (top && left) put(x, y, set.tl);
  else if (top) put(x, y, set.t);
  else if (left) put(x, y, set.l);
  if (top && right) put(x2, y, set.tr);
  else if (top) put(x2, y, set.t);
  else if (right) put(x2, y, set.r);
  if (bottom && left) put(x, y2, set.bl);
  else if (bottom) put(x, y2, set.b);
  else if (left) put(x, y2, set.l);
  if (bottom && right) put(x2, y2, set.br);
  else if (bottom) put(x2, y2, set.b);
  else if (right) put(x2, y2, set.r);
  for (let xx = x + 1; xx < x2; xx++) {
    if (top) put(xx, y, set.t);
    if (bottom) put(xx, y2, set.b);
  }
  for (let yy = y + 1; yy < y2; yy++) {
    if (left) put(x, yy, set.l);
    if (right) put(x2, yy, set.r);
  }
}

function fillBg(
  x: number, y: number, w: number, h: number, id: number,
  surface: Surface, noSelect: Uint8Array, noSel: boolean,
): void {
  for (let yy = y; yy < y + h; yy++) {
    for (let xx = x; xx < x + w; xx++) {
      surface.setCell(xx, yy, 32, id, 0);
      if (noSel) markNoSelect(noSelect, surface, xx, yy);
    }
  }
}

function paintNode(
  node: VNode, ax: number, ay: number,
  surface: Surface, palette: Palette, noSelect: Uint8Array, inheritNoSel: boolean,
  bg: string | undefined, softWrap: Uint8Array | undefined, cursor: CursorOut | undefined,
): void {
  const yn = node.yoga as YogaGeom | null;
  if (!yn) return;
  const w = Math.round(yn.getComputedWidth());
  const h = Math.round(yn.getComputedHeight());
  if (!(w > 0) || !(h > 0)) return; // also catches NaN (no layout) and display:none
  const x = ax + Math.round(yn.getComputedLeft());
  const y = ay + Math.round(yn.getComputedTop());
  const p = node.props;
  const noSel = inheritNoSel || p['selectable'] === false;

  if (node.type === 'ink-text') {
    paintText(node, x, y, w, h, surface, palette, noSelect, noSel, bg, softWrap, cursor);
    return;
  }

  const ownBg = typeof p['backgroundColor'] === 'string' ? (p['backgroundColor'] as string) : undefined;
  if (ownBg) fillBg(x, y, w, h, palette.intern({bg: ownBg}), surface, noSelect, noSel);
  if (p['borderStyle']) drawBorder(node, x, y, w, h, surface, palette, noSelect);

  const childBg = ownBg ?? bg; // nearest-ancestor background flows down to text
  for (const child of node.children) {
    if (child.kind === 'element') paintNode(child, x, y, surface, palette, noSelect, noSel, childBg, softWrap, cursor);
  }
}

/**
 * Render a laid-out vnode tree into `surface` (full frame: clears first) and return the NoSelect mask
 * (1 = excluded from copy). Children are drawn after their parent's background/border, so they paint
 * on top. If `softWrap` (one byte per surface row) is given, rows that are a soft-wrap continuation of
 * the row above (a long line that wrapped, NOT a hard line break) are marked 1 — selection/copy uses
 * it to re-join a wrapped logical line.
 */
export function paint(
  root: VNode,
  surface: Surface,
  palette: Palette,
  yOffset = 0,
  softWrap?: Uint8Array,
  cursor?: CursorOut,
): Uint8Array {
  surface.clear();
  const noSelect = new Uint8Array(surface.width * surface.height);
  // a positive yOffset scrolls the content up (the top rows fall off, the bottom stays on screen),
  // so a tree taller than the surface keeps its bottom (input + footer) visible instead of clipping it
  paintNode(root, 0, -Math.round(yOffset), surface, palette, noSelect, false, undefined, softWrap, cursor);
  return noSelect;
}
