/**
 * Split a committed turn body into ordered markdown + tool-call segments (#1167, epic #1144).
 *
 * The engine streams a tool call as `  ● <label>` followed by its result as `  ⎿ <first>` + `     <cont>`
 * lines (ANSI-coloured). Today that arrives as raw text inside the markdown block; splitting it out lets the
 * client render each tool call as a foldable component (collapsed by default, click to expand) while the rest
 * stays markdown. This module is the pure, testable parser — no rendering, no wiring.
 */
const ANSI = /\x1b\[[0-9;]*m/g;
const strip = (s: string): string => s.replace(ANSI, '');

// The engine's tool-call markers (gx10.py `_tool_display` / `_tool_result_lines`): a `  ● ` header, a
// `  ⎿ ` first result line, and `     ` (5-space) continuation lines.
const HEADER = /^ {2}● (.+)$/;
const RESULT_FIRST = /^ {2}⎿ (.*)$/;
const RESULT_CONT = /^ {5}(.*)$/;

// #1196: a coloured result line (`ls --color`) is streamed by the engine WITHOUT the grey wrap, so its
// `  ⎿ `/`     ` prefix stays plain at the start and the marker matches the RAW line — capturing the
// content WITH its inner SGR. A plain line is grey-wrapped, so its prefix only shows after stripping.
// Prefer the raw capture (keeps colour), fall back to the stripped one (plain, was grey-wrapped).
function capture(re: RegExp, raw: string, stripped: string): string | null {
  const m = re.exec(raw) ?? re.exec(stripped);
  return m ? (m[1] ?? '') : null;
}
function matches(re: RegExp, raw: string, stripped: string): boolean {
  return re.test(raw) || re.test(stripped);
}

export type Segment =
  | {type: 'md'; text: string}
  | {type: 'tool'; label: string; result: string[]};

export function splitToolBlocks(body: string): Segment[] {
  const raw = body.split('\n');
  const lines = raw.map(strip); // stripped view, for structure detection
  const segs: Segment[] = [];
  let md: string[] = [];
  const flushMd = (): void => {
    if (md.length) {
      segs.push({type: 'md', text: md.join('\n')});
      md = [];
    }
  };
  for (let i = 0; i < lines.length; i++) {
    // the header label is always plain (built from _tool_display), so the stripped capture is correct
    const h = HEADER.exec(lines[i] ?? '');
    if (h) {
      const result: string[] = [];
      let j = i + 1;
      const first = j < lines.length ? capture(RESULT_FIRST, raw[j] ?? '', lines[j] ?? '') : null;
      if (first !== null) {
        result.push(first);
        j += 1;
        while (j < lines.length) {
          const c = capture(RESULT_CONT, raw[j] ?? '', lines[j] ?? '');
          if (c !== null) {
            result.push(c);
            j += 1;
            continue;
          }
          // A blank line belongs to the result only if a `     ` continuation still follows: the router
          // collapses a `     ` empty line (e.g. PowerShell output starts with one) to '', which would
          // otherwise end the block and leak the rest of the output into a markdown segment.
          if ((lines[j] ?? '').trim() === '') {
            let k = j;
            while (k < lines.length && (lines[k] ?? '').trim() === '') k += 1;
            if (k < lines.length && matches(RESULT_CONT, raw[k] ?? '', lines[k] ?? '')) {
              for (; j < k; j += 1) result.push('');
              continue;
            }
          }
          break;
        }
      }
      flushMd();
      segs.push({type: 'tool', label: h[1] ?? '', result});
      i = j - 1;
      continue;
    }
    md.push(lines[i] ?? '');
  }
  flushMd();
  return segs;
}
