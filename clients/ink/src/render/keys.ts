/**
 * keys — parse a raw stdin chunk into an Ink-compatible `(input, Key)` event (R5).
 *
 * In raw mode each keypress arrives as one `data` chunk: a printable string, a single control
 * byte, or an ANSI escape sequence. This classifies the common set Ink components rely on —
 * arrows, navigation (page/delete), enter/tab/escape/backspace, Ctrl+letter, and Alt(meta)+key —
 * plus xterm modifier params (Shift/Alt/Ctrl on arrows, e.g. `ESC [ 1 ; 5 D` = Ctrl+Left). Anything
 * else (a letter, or a whole paste) is returned verbatim as `input` with an all-false key.
 *
 * Scope note: a chunk is treated as one keypress (Ink's model). Splitting a batched chunk into
 * several keys is the dispatcher's concern (dispatch.ts), not this pure classifier's.
 */
import {emptyKey, type Key} from './hooks.js';

export interface ParsedKey {
  input: string;
  key: Key;
}

const ESC = '\x1b';

/** CSI final letter → arrow. */
const CSI_ARROW: Record<string, Partial<Key>> = {
  A: {upArrow: true},
  B: {downArrow: true},
  C: {rightArrow: true},
  D: {leftArrow: true},
};

/** CSI `<n> ~` → navigation key. */
const CSI_TILDE: Record<string, Partial<Key>> = {
  '3': {delete: true},
  '5': {pageUp: true},
  '6': {pageDown: true},
};

/** Decode an xterm modifier param (1 + shift|alt<<1|ctrl<<2) onto a key. */
function applyModifier(key: Key, mod: number): void {
  const bits = mod - 1;
  if (bits & 1) key.shift = true;
  if (bits & 2) key.meta = true;
  if (bits & 4) key.ctrl = true;
}

/** Classify one raw stdin chunk. */
export function parseKey(data: string): ParsedKey {
  const key = emptyKey();

  // single control characters with a dedicated meaning
  if (data === '\r' || data === '\n') {
    key.return = true;
    return {input: '', key};
  }
  if (data === '\t') {
    key.tab = true;
    return {input: '', key};
  }
  if (data === '\x7f' || data === '\x08') {
    key.backspace = true;
    return {input: '', key};
  }
  if (data === ESC) {
    key.escape = true;
    return {input: '', key};
  }

  // CSI / SS3 escape sequences: ESC [ … final  or  ESC O final
  if (data.startsWith(ESC + '[') || data.startsWith(ESC + 'O')) {
    const body = data.slice(2);
    const m = /^(\d*)(?:;(\d+))?([A-Z~])$/.exec(body);
    if (m) {
      const num = m[1] ?? '';
      const mod = m[2] ? parseInt(m[2], 10) : 0;
      const final = m[3] ?? '';
      if (final === '~') {
        const nav = CSI_TILDE[num];
        if (nav) Object.assign(key, nav);
      } else {
        const arrow = CSI_ARROW[final];
        if (arrow) Object.assign(key, arrow);
        // home/end (H/F) etc. have no Ink Key field → swallowed
      }
      if (mod) applyModifier(key, mod);
    }
    return {input: '', key};
  }

  // Alt/Meta: ESC + a following key (not a CSI, handled above)
  if (data.length >= 2 && data[0] === ESC) {
    const inner = parseKey(data.slice(1));
    return {input: inner.input, key: {...inner.key, meta: true}};
  }

  // Ctrl + letter: a lone byte 1..26 (8/9/13 already consumed as backspace/tab/return)
  if (data.length === 1) {
    const code = data.charCodeAt(0);
    if (code >= 1 && code <= 26) {
      key.ctrl = true;
      return {input: String.fromCharCode(code + 96), key}; // 1→'a' … 26→'z'
    }
  }

  // printable character, or a whole paste
  return {input: data, key};
}

/** Parse a chunk and hand the event to the input bridge (mount/dispatch wires this to stdin). */
export function feedKey(data: string, emit: (input: string, key: Key) => void): void {
  const {input, key} = parseKey(data);
  emit(input, key);
}
