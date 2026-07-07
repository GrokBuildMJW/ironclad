/**
 * Markdown → ANSI for the terminal, via marked + marked-terminal.
 *
 * Key for Yoga: we pre-wrap the prose to the target column width (`reflowText`,
 * `width`), so the ANSI string handed to Ink's <Text> is already line-broken — Ink/Yoga
 * never has to wrap a line mid-ANSI-escape (which is what corrupts colours/layout).
 * A fresh Marked instance per call avoids the extension-stacking that marked.use() causes.
 */
import './forceColor.js'; // MEM-20: must run before marked-terminal so chalk inits with colour on
import {Marked} from 'marked';
import {markedTerminal} from 'marked-terminal';

// #1145 (epic #1144): a muted, Claude-Code-like palette instead of marked-terminal's colourful DEFAULT
// (green headings / yellow code / blue-underlined links / red table headers). Raw ANSI style functions so
// NO extra dependency is added — restrained emphasis (bold / dim / italic) reads calmer in the chat pane.
const _bold = (s: string): string => `\x1b[1m${s}\x1b[22m`;
const _dim = (s: string): string => `\x1b[2m${s}\x1b[22m`;
const _indigo = (s: string): string => `\x1b[38;2;129;140;248m${s}\x1b[39m`; // #1156: inline code + links (indigo #818cf8)
const _plain = (s: string): string => s;

// #1146 (epic #1144): a blockquote as a left-bar on its OWN line (Claude-Code style `▎ …`), instead of
// marked-terminal's inline italic that glues to the preceding list/paragraph line.
const _blockquoteBar = (t: string): string =>
  t
    .split('\n')
    .map((l) => (l.trim() ? `\x1b[2m▎ ${l.replace(/^\s+/, '')}\x1b[22m` : l))
    .join('\n');

// A blockquote that starts right after non-blockquote content (no blank line) is otherwise merged onto that
// line — insert the blank line so it renders as its own block. Multi-line blockquotes (`>` after `>`) untouched.
function _blankBeforeBlockquote(md: string): string {
  const lines = md.split('\n');
  const out: string[] = [];
  for (let i = 0; i < lines.length; i++) {
    const cur = lines[i] ?? '';
    const prev = i > 0 ? (lines[i - 1] ?? '') : '';
    if (/^\s*>/.test(cur) && prev.trim() !== '' && !/^\s*>/.test(prev)) out.push('');
    out.push(cur);
  }
  return out.join('\n');
}

// #1152 (epic #1144): a smaller model often emits a pipe-table WITHOUT the `|---|` separator row, which GFM
// then renders as flat pipe-text instead of a box. Repair it — insert a separator after the first row of a
// pipe block that lacks one — so a common model imperfection still renders as a table. Well-formed tables
// (header already followed by a separator) and non-table pipe text are untouched.
function _repairPipeTables(md: string): string {
  const isRow = (l: string): boolean => /^\s*\|.*\|\s*$/.test(l);
  const isSep = (l: string): boolean => /^\s*\|(?:\s*:?-{2,}:?\s*\|)+\s*$/.test(l);
  const lines = md.split('\n');
  const out: string[] = [];
  for (let i = 0; i < lines.length; i++) {
    const cur = lines[i] ?? '';
    const prev = i > 0 ? (lines[i - 1] ?? '') : '';
    const next = lines[i + 1] ?? '';
    out.push(cur);
    if (isRow(cur) && !isSep(cur) && !isRow(prev) && isRow(next) && !isSep(next)) {
      const cols = cur.split('|').length - 2; // "| a | b |" → 4 pipes-1 → 3 splits inner → 2 cols
      out.push('|' + ' --- |'.repeat(Math.max(1, cols)));
    }
  }
  return out.join('\n');
}

// Normalise marked-terminal's list rendering to Claude Code's: its 4-space-per-level "* " / "N. " becomes
// a "- " (and "N. ") bullet at (level-1)*2 spaces — dash markers, tight indent, top level at column 0.
function _normaliseLists(s: string): string {
  const indent = (sp: string): string => '  '.repeat(Math.max(0, Math.round(sp.length / 4) - 1));
  return s
    .replace(/^( *)\* /gm, (_m, sp: string) => indent(sp) + '- ')
    .replace(/^( *)(\d+)\. /gm, (_m, sp: string, n: string) => indent(sp) + n + '. ');
}

/** Markdown syntax probe — if a block matches none of this, it renders verbatim (the fast path).
 * Includes GFM table rows (a line starting with `|`) so decision-matrix tables go through the parser
 * instead of being printed verbatim as flow text (LOK-6). */
const HAS_MARKDOWN = /[*_`~#]|\[[^\]]*\]\(|^\s{0,3}([-+*]|\d+\.)\s|^\s{0,3}>|^\s{0,3}#{1,6}\s|^\s{0,3}\||```/m;

/** Count fence lines (``` possibly indented) in a chunk — an odd count toggles the fence open/closed. */
function fenceCount(s: string): number {
  return (s.match(/^[ \t]*```/gm) ?? []).length;
}

/**
 * Split a body into blank-line-separated blocks for incremental rendering — but NEVER split inside
 * an open ``` fence (MEM-20). A fenced code block that contains a blank line would otherwise be cut
 * across blocks, breaking the fence so each half parses on its own and the live preview garbles the
 * code. While a fence is open, following chunks are merged until it closes; an unterminated fence
 * stays as the still-growing tail.
 */
export function splitBlocks(body: string): string[] {
  const parts = body.split(/\n{2,}/);
  const blocks: string[] = [];
  let buf: string[] = [];
  let open = false;
  for (const p of parts) {
    buf.push(p);
    if (fenceCount(p) % 2 === 1) open = !open;
    if (!open) {
      blocks.push(buf.join('\n\n'));
      buf = [];
    }
  }
  if (buf.length) blocks.push(buf.join('\n\n')); // unterminated fence → the open tail
  return blocks;
}

/**
 * Streaming markdown renderer (concept §9). For a live token stream, re-parsing the whole answer on
 * every token is wasteful, so this splits the body into blank-line-separated blocks and:
 *  - **caches** complete (non-tail) blocks by content, so earlier text is never re-parsed;
 *  - **fast-paths** a block with no markdown syntax straight through (no parser);
 *  - only the **open tail** (the last, still-growing block) is re-rendered each call.
 * The final, committed answer should still use `renderMarkdown(fullBody)` for an exact whole-document
 * render; this is the fast live preview while the tokens arrive.
 */
export class StreamMarkdown {
  private cache = new Map<string, string>();

  constructor(private readonly width = 80) {}

  reset(): void {
    this.cache.clear();
  }

  /** Number of completed blocks currently cached (the open tail is never cached). */
  get cachedBlocks(): number {
    return this.cache.size;
  }

  render(body: string): string {
    const blocks = splitBlocks(body); // MEM-20: fence-aware so code blocks aren't cut at blank lines
    const out: string[] = [];
    for (let i = 0; i < blocks.length; i++) {
      const text = blocks[i] ?? '';
      if (i < blocks.length - 1) {
        let r = this.cache.get(text);
        if (r === undefined) {
          r = this.renderBlock(text);
          this.cache.set(text, r);
        }
        out.push(r);
      } else {
        out.push(this.renderBlock(text)); // the open tail, still streaming
      }
    }
    return out.join('\n\n');
  }

  private renderBlock(text: string): string {
    if (!text.trim()) return '';
    if (!HAS_MARKDOWN.test(text)) return text; // plain prose → verbatim, no parser cost
    return renderMarkdown(text, this.width);
  }
}

export function renderMarkdown(md: string, width = 80): string {
  const w = Math.max(8, Math.floor(width));
  // `breaks: true` keeps the model's single newlines as hard line breaks — chat output (and
  // line-structured replies like `status`) read as written, instead of being reflowed into one
  // run-on paragraph. Long lines are still wrapped to the width by marked-terminal.
  const m = new Marked(
    {breaks: true, gfm: true},
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    markedTerminal({
      width: w,
      reflowText: true,
      showSectionPrefix: false, // #1145: no literal "##"/"###" prefix — bold heading only
      firstHeading: _bold,
      heading: _bold, // bold, not green
      code: _dim,
      codespan: _indigo, // #1146: inline code in cyan (Claude-Code), not yellow
      blockquote: _blockquoteBar, // #1146: left-bar "▎ " on its own line, Claude-Code style
      link: _indigo, // #1146: link text in cyan
      href: _indigo, // #1146: url in cyan, not bright blue + underline
      listitem: _plain,
      tableOptions: {style: {head: [], border: []}}, // drop the red header / grey border colour
    }) as any,
  );
  const out = m.parse(_blankBeforeBlockquote(_repairPipeTables(md))); // #1146/#1152: block `>` split + repair separator-less tables
  const s = typeof out === 'string' ? out : String(out);
  return _normaliseLists(s.replace(/\n+$/, '')); // #1146: dash bullets + tight indent, Claude-Code style
}
