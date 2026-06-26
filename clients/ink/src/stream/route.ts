/**
 * Output filter — a byte-exact port of engine/cli.py:_stream_turn.route.
 *
 * Streamed text is split into lines; each line is routed:
 *  1. `[perf] …` → the status footer (model perf + token count), NOT the chat.
 *  2. `======== ✓ DONE … ========` banner → dropped.
 *  3. role labels `[GX10]` / `[Qwen (planning)]` / `[… planning …]` → dropped.
 *  4. runs of blank lines collapse to one (kept for Markdown paragraph spacing).
 *  5. everything else accumulates into the answer (rendered as one Markdown block).
 *
 * feed()/flush() line-buffer raw stream chunks (chatStream's onText delivers chunks, not
 * lines); route() is the pure per-line core (unit-tested).
 */
const ANSI = new RegExp(String.fromCharCode(27) + '\\[[0-9;]*m', 'g');
const TOK = /(\d+)\s*tok/;
const PERF = '[perf]';
const AGENT = '[agent]'; // #453: routing provenance → which coder was called (footer, not chat)
const SEARCH = '[search]'; // epic #505 S9: web-search summary (n batches + ms) → footer, not chat
// MPR report sentinels (skills/mpr/entry.py REPORT_OPEN/CLOSE) — machine delimiters that mark the
// verbatim report block; they must never reach the rendered chat (#50).
const REPORT_OPEN = '<<<MPR_REPORT>>>';
const REPORT_CLOSE = '<<<END>>>';

export interface Router {
  route: (line: string) => void;
  feed: (chunk: string) => void;
  flush: () => void;
  answer: string[];
  perf: string;
  tokens: number;
  agent: string;
  search: string;
}

export function createRouter(): Router {
  const answer: string[] = [];
  let blank = true;
  let lineBuf = '';

  const state: Router = {
    answer,
    perf: '',
    tokens: 0,
    agent: '',
    search: '',
    route: () => {},
    feed: () => {},
    flush: () => {},
  };

  state.route = (line: string): void => {
    const st = line.replace(ANSI, '').trim();

    const i = st.indexOf(PERF);
    if (i !== -1) {
      state.perf = st.slice(i + PERF.length).trim();
      const m = TOK.exec(state.perf);
      if (m) state.tokens = parseInt(m[1] ?? '0', 10);
      return;
    }
    const ai = st.indexOf(AGENT); // #453: which coder was routed → footer, dropped from the chat
    if (ai !== -1) {
      state.agent = st.slice(ai + AGENT.length).trim();
      return;
    }
    const si = st.indexOf(SEARCH); // S9: web-search summary → footer, dropped from the chat
    if (si !== -1) {
      state.search = st.slice(si + SEARCH.length).trim();
      return;
    }
    if (st.includes('===') && (st.includes('DONE') || st.includes('✓'))) return;
    // MPR sentinels on their own line (trim() also catches the model's indented/glued `<<<END>>>`).
    if (st === REPORT_OPEN || st === REPORT_CLOSE) return;
    if (
      st === '[GX10]' ||
      (st.startsWith('[Qwen') && st.endsWith(']')) ||
      (st.startsWith('[') && st.includes('planning') && st.endsWith(']'))
    ) {
      return;
    }
    if (!st) {
      if (blank) return;
      blank = true;
      answer.push('');
      return;
    }
    blank = false;
    answer.push(line);
  };

  state.feed = (chunk: string): void => {
    lineBuf += chunk;
    let nl: number;
    while ((nl = lineBuf.indexOf('\n')) !== -1) {
      state.route(lineBuf.slice(0, nl));
      lineBuf = lineBuf.slice(nl + 1);
    }
  };

  state.flush = (): void => {
    if (lineBuf) {
      state.route(lineBuf);
      lineBuf = '';
    }
  };

  return state;
}

/** The accumulated answer joined into a single markdown body (trailing blanks trimmed). */
export function answerBody(r: Router): string {
  return r.answer.join('\n').replace(/\n+$/, '');
}
