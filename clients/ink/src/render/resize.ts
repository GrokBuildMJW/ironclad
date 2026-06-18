/**
 * resize — terminal size watching + the erase sequences for ghost-free resize (R7, pattern §9).
 *
 * A resize is handled synchronously: read the new size, resize the buffers (full taint), clear the
 * old frame, and repaint. The clear is what kills ghosts — Stock Ink's `eraseLines(count)` miscounts
 * when the width changed, leaving stragglers. Two erase strategies:
 *  - `clearScreen()` — for the alternate screen (decision a): wipe the whole buffer + home the
 *    cursor, then a full repaint lands cleanly.
 *  - `eraseFrame(rows)` — for inline/main-screen rendering: return to the frame's top-left and clear
 *    to end of screen (`ESC[J`), which is width-change robust (unlike line-count erasing).
 *
 * The watcher is `process.stdout`'s `resize` event (emitted on SIGWINCH); size reads fall back to a
 * sane 80x24 when not a TTY.
 */

export interface Size {
  columns: number;
  rows: number;
}

/** Current terminal size with non-TTY fallbacks. */
export function terminalSize(stdout: NodeJS.WriteStream): Size {
  return {columns: stdout.columns || 80, rows: stdout.rows || 24};
}

/** Subscribe to terminal resizes; returns an unsubscribe. Fires with the new size. */
export function watchResize(stdout: NodeJS.WriteStream, onResize: (size: Size) => void): () => void {
  const handler = (): void => onResize(terminalSize(stdout));
  stdout.on('resize', handler);
  return () => {
    stdout.off('resize', handler);
  };
}

/** Wipe the whole screen and home the cursor (alternate-screen repaint). */
export function clearScreen(): string {
  return '\x1b[2J\x1b[H';
}

/**
 * Return to the top-left of a `rows`-tall inline frame and clear to end of screen. Width-change
 * robust: `ESC[J` clears everything below+right of the cursor regardless of the old line widths.
 */
export function eraseFrame(rows: number): string {
  if (rows <= 1) return '\r\x1b[J';
  return `\r\x1b[${rows - 1}A\x1b[J`;
}
