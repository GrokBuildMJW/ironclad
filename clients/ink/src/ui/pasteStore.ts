/**
 * Multi-line paste compression for the chat input (#438) — a hardened port of the old Python TUI model
 * (`engine/tui.py`, since retired). A bracketed / right-click paste of more than one line is stored
 * and shown as a compact `[Pasted #N +L lines]` placeholder, then expanded back to the raw text when the
 * turn is submitted — like Claude Code — so a big paste never floods the input line or the transcript.
 *
 * Robustness (from the #438 adversarial review):
 *  - **Out-of-band token.** The text the user SEES is `[Pasted #N +L lines]`, but what lives in the buffer
 *    is a sentinel `OPEN<id>:<L>CLOSE` built from Private-Use-Area code points (U+E000/U+E001) the user
 *    cannot type. Expansion and Backspace key on that sentinel — never on the visible grammar — so a user
 *    who literally types or single-line-pastes "[Pasted #1 +2 lines]" is never mistaken for a real paste
 *    (no silent round-trip corruption, no whole-token over-delete).
 *  - **Reclaim.** Blocks live in a `Map<id, raw>`; deleting a collapsed paste drops its block (no leak).
 *    Ids are monotonic and never reused, so a placeholder's id is stable even after earlier ones go away.
 *  - **Newline-agnostic.** LF, CRLF and lone CR all count as line breaks and are normalized in storage.
 *
 * Pure/framework-free so it is unit-tested directly; `App.tsx` owns one {@link PasteStore} per turn.
 */

// PUA delimiters (U+E000/U+E001) — not typeable, so a sentinel token can never be forged from input.
// Built from code points (not literal chars) so the source stays ASCII-clean and unambiguous.
const OPEN = String.fromCodePoint(0xe000);
const CLOSE = String.fromCodePoint(0xe001);
const TOKEN_BODY = '(\\d+):(\\d+)'; // <id>:<lineCount>
/** A whole sentinel token; fresh each call so callers never share a global `lastIndex`. */
const tokenRe = (): RegExp => new RegExp(OPEN + TOKEN_BODY + CLOSE, 'g');
const trailingTokenRe = new RegExp(OPEN + TOKEN_BODY + CLOSE + '$');

export interface PasteStore {
  seq: number; // monotonic id source — ids are stable and never reused (no reindex on delete)
  blocks: Map<number, string>; // id -> raw pasted text
}

export function newPasteStore(): PasteStore {
  return {seq: 0, blocks: new Map()};
}

/** Normalize CRLF and lone CR to LF (so detection, counting and storage are separator-agnostic). */
function normalizeNewlines(text: string): string {
  return text.replace(/\r\n?/g, '\n');
}

/** A paste is worth compressing iff it spans more than one line, ignoring leading/trailing whitespace. */
export function isMultilinePaste(text: string): boolean {
  return normalizeNewlines(text).trim().includes('\n');
}

/** The line count shown in the placeholder: newlines + 1 (LF/CRLF/CR all counted; matches the old tui.py). */
export function pasteLineCount(text: string): number {
  return (normalizeNewlines(text).match(/\n/g)?.length ?? 0) + 1;
}

/** The human-readable placeholder for block `n` covering `lines` lines (what the user SEES, not stored). */
export function pastePlaceholder(n: number, lines: number): string {
  return `[Pasted #${n} +${lines} lines]`;
}

/** Store a raw paste (newlines normalized) and return the OUT-OF-BAND buffer token to insert. */
export function storePaste(store: PasteStore, text: string): string {
  const id = ++store.seq;
  store.blocks.set(id, normalizeNewlines(text));
  return `${OPEN}${id}:${pasteLineCount(text)}${CLOSE}`;
}

/** Render a buffer for display: each sentinel token becomes the friendly `[Pasted #N +L lines]`, and any
 * residual typed newline is compacted to a ⏎ glyph (LOK-5) so the input box never grows a row. Pure. */
export function displayBuffer(buffer: string): string {
  return buffer
    .replace(tokenRe(), (_w, id: string, lines: string) => pastePlaceholder(Number(id), Number(lines)))
    .replace(/\r?\n/g, ' ⏎ ');
}

/** Expand every sentinel token to its stored raw block for submission. A token whose id is no longer in
 * the store (should not happen) falls back to its friendly text so user content is never dropped. */
export function expandPastes(buffer: string, store: PasteStore): string {
  return buffer.replace(tokenRe(), (_w, id: string, lines: string) =>
    store.blocks.get(Number(id)) ?? pastePlaceholder(Number(id), Number(lines)));
}

/** Strip the sentinel delimiters from untrusted input. A user cannot TYPE U+E000/U+E001, but a paste
 * could carry them verbatim — removing them before the text reaches the buffer makes a sentinel
 * unforgeable (closes the residual the out-of-band scheme would otherwise open). The chars are
 * non-printable PUA, so dropping them never harms real content. */
export function stripSentinels(text: string): string {
  return text.replace(new RegExp(`[${OPEN}${CLOSE}]`, 'g'), '');
}

/** Backspace: if the buffer ends in a whole sentinel token, drop the WHOLE token and reclaim its block
 * (one keypress clears a collapsed paste); otherwise drop a single character. */
export function backspace(buffer: string, store: PasteStore): string {
  const m = buffer.match(trailingTokenRe);
  if (m) {
    store.blocks.delete(Number(m[1]));
    return buffer.slice(0, buffer.length - m[0].length);
  }
  return buffer.slice(0, -1);
}
