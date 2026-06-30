/**
 * layout — Yoga (yoga-layout@3, MIT) flexbox layout for the vnode tree (R3b).
 *
 * Maps a vnode's visual props (the full common Box style set) onto a Yoga node, attaches a
 * measure function to text nodes (CJK/emoji-aware width + word-wrap with char-break fallback),
 * builds the Yoga tree alongside the vnode tree, runs calculateLayout, and frees nodes on
 * teardown. paint.ts reads the computed geometry straight off `vnode.yoga`.
 *
 * yoga-layout@3 is usable synchronously (the default export is pre-loaded) — no async init.
 */
import Yoga, {Edge, FlexDirection, Align, Justify, Wrap, Display, PositionType, Gutter, MeasureMode, Direction} from 'yoga-layout';
import {charWidth} from './surface.js';
import type {VNode} from './vnode.js';

type YogaNode = ReturnType<typeof Yoga.Node.create>;
type YogaConfig = ReturnType<typeof Yoga.Config.create>;

export function createConfig(): YogaConfig {
  const cfg = Yoga.Config.create();
  cfg.setPointScaleFactor(1); // terminal cells are integers, not sub-pixel
  cfg.setUseWebDefaults(false);
  return cfg;
}

// ── text width + word-wrap (CJK/emoji aware) ────────────────────────────────
// INK-R-1 (#503): ANSI CSI/SGR escapes carry ZERO display width — paint.ts consumes them into styled
// runs (its SGR_RE/CSI_RE) and wraps the *visible* glyphs, so the layout measure must not count their
// bytes as columns. If it does, a colored transcript line is over-measured, Yoga reserves the wrong
// geometry, and the line is re-wrapped/clipped against paint. Scope mirrors paint: SGR is a CSI ending
// in 'm'; any other CSI in content is dropped there too.
const ANSI_RE = /\x1b\[[0-9;?]*[ -/]*[@-~]/g;

/** Strip ANSI CSI/SGR escapes so width/wrap measurement counts visible columns only (INK-R-1). */
export function stripAnsi(s: string): string {
  return s.includes('\x1b') ? s.replace(ANSI_RE, '') : s;
}

/** Display width of a string in terminal columns (sum of per-code-point widths; ANSI escapes ignored). */
export function textWidth(s: string): number {
  let w = 0;
  for (const ch of stripAnsi(s)) w += charWidth(ch.codePointAt(0) ?? 32);
  return w;
}

/**
 * Word-wrap `text` to `width` columns. Wraps at spaces; a word wider than `width` is broken
 * char-by-char (so CJK runs and over-long tokens still fit). Honors explicit '\n'. width<=0 or
 * Infinity → no wrapping (split on '\n' only).
 */
export function wrapText(text: string, width: number): string[] {
  text = stripAnsi(text); // INK-R-1 (#503): wrap by visible width — escapes must not consume budget or be split
  if (!Number.isFinite(width) || width <= 0) return text.split('\n');
  const out: string[] = [];
  for (const para of text.split('\n')) {
    const start = out.length;
    let line = '';
    let lineW = 0;
    const flush = (): void => {
      out.push(line.replace(/[ \t]+$/, '')); // wrap point → drop the dangling space
      line = '';
      lineW = 0;
    };
    // tokens alternate word / whitespace; keep them so inter-word spacing survives
    for (const tok of para.match(/\s+|\S+/g) ?? ['']) {
      const tw = textWidth(tok);
      if (lineW + tw <= width) {
        line += tok;
        lineW += tw;
        continue;
      }
      if (/^\s+$/.test(tok)) {
        if (line) flush(); // overflowing whitespace → break the line, drop the space
        continue;
      }
      if (tw > width) {
        for (const ch of tok) {
          // word longer than a full line → break it char by char
          const cw = charWidth(ch.codePointAt(0) ?? 32);
          if (lineW + cw > width && line) flush();
          line += ch;
          lineW += cw;
        }
      } else {
        if (line) flush();
        line = tok;
        lineW = tw;
      }
    }
    // final line keeps its trailing whitespace (intentional inline spacing, e.g. "model " +
    // a sibling segment); emit an empty final line only for a genuinely blank paragraph.
    if (line.length > 0 || out.length === start) out.push(line);
  }
  return out;
}

/** Concatenate the text content of an ink-text node (raw text + nested ink-text children). */
export function collectText(node: VNode): string {
  let s = '';
  for (const c of node.children) {
    if (c.kind === 'text') s += c.value;
    else if (c.type === 'ink-text') s += collectText(c);
  }
  return s;
}

// ── style → Yoga ────────────────────────────────────────────────────────────
const FLEX_DIR: Record<string, FlexDirection> = {
  row: FlexDirection.Row, column: FlexDirection.Column,
  'row-reverse': FlexDirection.RowReverse, 'column-reverse': FlexDirection.ColumnReverse,
};
const ALIGN: Record<string, Align> = {
  'flex-start': Align.FlexStart, center: Align.Center, 'flex-end': Align.FlexEnd,
  stretch: Align.Stretch, 'space-between': Align.SpaceBetween, 'space-around': Align.SpaceAround,
  baseline: Align.Baseline, auto: Align.Auto,
};
const JUSTIFY: Record<string, Justify> = {
  'flex-start': Justify.FlexStart, center: Justify.Center, 'flex-end': Justify.FlexEnd,
  'space-between': Justify.SpaceBetween, 'space-around': Justify.SpaceAround, 'space-evenly': Justify.SpaceEvenly,
};

function setDim(v: unknown, px: (n: number) => void, pct: (n: number) => void, auto: (() => void) | null): void {
  if (typeof v === 'number') px(v);
  else if (typeof v === 'string') {
    if (v === 'auto' && auto) auto();
    else if (v.endsWith('%')) pct(parseFloat(v));
  }
}

const EDGES: Array<[string, Edge]> = [['Top', Edge.Top], ['Bottom', Edge.Bottom], ['Left', Edge.Left], ['Right', Edge.Right]];

function applyEdges(p: Record<string, unknown>, key: string, set: (e: Edge, v: number) => void): void {
  const all = p[key];
  if (typeof all === 'number') EDGES.forEach(([, e]) => set(e, all));
  const x = p[key + 'X'];
  if (typeof x === 'number') { set(Edge.Left, x); set(Edge.Right, x); }
  const y = p[key + 'Y'];
  if (typeof y === 'number') { set(Edge.Top, y); set(Edge.Bottom, y); }
  for (const [suf, e] of EDGES) {
    const v = p[key + suf];
    if (typeof v === 'number') set(e, v);
  }
}

/** Apply a vnode's visual props to its Yoga node. */
export function applyStyle(node: YogaNode, p: Record<string, unknown>): void {
  if (typeof p['flexDirection'] === 'string') node.setFlexDirection(FLEX_DIR[p['flexDirection']] ?? FlexDirection.Column);
  if (typeof p['flexGrow'] === 'number') node.setFlexGrow(p['flexGrow']);
  if (typeof p['flexShrink'] === 'number') node.setFlexShrink(p['flexShrink']);
  if (p['flexBasis'] !== undefined) setDim(p['flexBasis'], (n) => node.setFlexBasis(n), (n) => node.setFlexBasisPercent(n), () => node.setFlexBasisAuto());
  if (typeof p['flexWrap'] === 'string') node.setFlexWrap(p['flexWrap'] === 'wrap' ? Wrap.Wrap : p['flexWrap'] === 'wrap-reverse' ? Wrap.WrapReverse : Wrap.NoWrap);
  if (typeof p['alignItems'] === 'string') node.setAlignItems(ALIGN[p['alignItems']] ?? Align.Stretch);
  if (typeof p['alignSelf'] === 'string') node.setAlignSelf(ALIGN[p['alignSelf']] ?? Align.Auto);
  if (typeof p['justifyContent'] === 'string') node.setJustifyContent(JUSTIFY[p['justifyContent']] ?? Justify.FlexStart);
  if (p['width'] !== undefined) setDim(p['width'], (n) => node.setWidth(n), (n) => node.setWidthPercent(n), () => node.setWidthAuto());
  if (p['height'] !== undefined) setDim(p['height'], (n) => node.setHeight(n), (n) => node.setHeightPercent(n), () => node.setHeightAuto());
  if (p['minWidth'] !== undefined) setDim(p['minWidth'], (n) => node.setMinWidth(n), (n) => node.setMinWidthPercent(n), null);
  if (p['minHeight'] !== undefined) setDim(p['minHeight'], (n) => node.setMinHeight(n), (n) => node.setMinHeightPercent(n), null);
  if (p['maxWidth'] !== undefined) setDim(p['maxWidth'], (n) => node.setMaxWidth(n), (n) => node.setMaxWidthPercent(n), null);
  if (p['maxHeight'] !== undefined) setDim(p['maxHeight'], (n) => node.setMaxHeight(n), (n) => node.setMaxHeightPercent(n), null);
  applyEdges(p, 'padding', (e, v) => node.setPadding(e, v));
  applyEdges(p, 'margin', (e, v) => node.setMargin(e, v));
  if (typeof p['gap'] === 'number') node.setGap(Gutter.All, p['gap']);
  if (typeof p['columnGap'] === 'number') node.setGap(Gutter.Column, p['columnGap']);
  if (typeof p['rowGap'] === 'number') node.setGap(Gutter.Row, p['rowGap']);
  if (p['display'] === 'none') node.setDisplay(Display.None);
  if (p['position'] === 'absolute') node.setPositionType(PositionType.Absolute);
  for (const [suf, e] of EDGES) {
    const v = p[suf.toLowerCase()]; // top/left/right/bottom for absolute positioning
    if (typeof v === 'number') node.setPosition(e, v);
  }
  // Ink border occupies 1 cell on each active edge (borderStyle present, edge not false).
  if (p['borderStyle']) {
    if (p['borderTop'] !== false) node.setBorder(Edge.Top, 1);
    if (p['borderBottom'] !== false) node.setBorder(Edge.Bottom, 1);
    if (p['borderLeft'] !== false) node.setBorder(Edge.Left, 1);
    if (p['borderRight'] !== false) node.setBorder(Edge.Right, 1);
  }
}

// ── tree build + layout ─────────────────────────────────────────────────────
/** Build (or rebuild) the Yoga tree for a vnode subtree, attaching nodes to `vnode.yoga`. */
export function attachYoga(vnode: VNode, cfg: YogaConfig): YogaNode {
  const yn = Yoga.Node.create(cfg);
  vnode.yoga = yn;
  applyStyle(yn, vnode.props);

  if (vnode.type === 'ink-text') {
    yn.setMeasureFunc((w, wm) => {
      const text = collectText(vnode);
      if (!text) return {width: 0, height: 0};
      const avail = wm === MeasureMode.Undefined ? Infinity : w;
      const lines = wrapText(text, avail);
      let max = 0;
      for (const l of lines) max = Math.max(max, textWidth(l));
      return {width: max, height: lines.length};
    });
    return yn; // text nodes don't get element children in the Yoga tree
  }

  let i = 0;
  for (const child of vnode.children) {
    if (child.kind === 'element') {
      const cn = attachYoga(child, cfg);
      yn.insertChild(cn, i++);
    }
  }
  return yn;
}

/** Run layout for a root vnode whose Yoga tree is attached. */
export function calculate(root: VNode, width: number, height?: number): void {
  (root.yoga as YogaNode).calculateLayout(width, height, Direction.LTR);
}

/** Free the Yoga nodes of a subtree (call on unmount to release WASM memory). */
export function freeYoga(vnode: VNode): void {
  if (vnode.yoga) {
    (vnode.yoga as YogaNode).freeRecursive();
    vnode.yoga = null;
  }
}
