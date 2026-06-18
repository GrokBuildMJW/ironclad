/**
 * Markdown → ANSI for the terminal, via marked + marked-terminal.
 *
 * Key for Yoga: we pre-wrap the prose to the target column width (`reflowText`,
 * `width`), so the ANSI string handed to Ink's <Text> is already line-broken — Ink/Yoga
 * never has to wrap a line mid-ANSI-escape (which is what corrupts colours/layout).
 * A fresh Marked instance per call avoids the extension-stacking that marked.use() causes.
 */
import {Marked} from 'marked';
import {markedTerminal} from 'marked-terminal';

/** Markdown syntax probe — if a block matches none of this, it renders verbatim (the fast path). */
const HAS_MARKDOWN = /[*_`~#]|\[[^\]]*\]\(|^\s{0,3}([-+*]|\d+\.)\s|^\s{0,3}>|^\s{0,3}#{1,6}\s|```/m;

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
    const blocks = body.split(/\n{2,}/);
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
    markedTerminal({width: w, reflowText: true}) as any,
  );
  const out = m.parse(md);
  const s = typeof out === 'string' ? out : String(out);
  return s.replace(/\n+$/, '');
}
