/**
 * safeCut — for live streaming markdown. Given a PARTIAL markdown buffer, return the index
 * up to which it is SAFE to render as a committed markdown block: the end of the last
 * paragraph boundary (a blank line) that sits OUTSIDE an open ``` / ~~~ code fence.
 *
 * Cutting only at blank lines inherently avoids the two hazards from the plan:
 *  - an open code fence (a fence is never terminated by a blank line mid-stream), and
 *  - a still-streaming table (table rows aren't followed by a blank line until done).
 *
 * Returns -1 when nothing is safely committable yet (caller falls back to render-once).
 */
const FENCE = /^ {0,3}(```|~~~)/;

export function safeCut(buf: string): number {
  const lines = buf.split('\n');
  const starts: number[] = [];
  let pos = 0;
  for (const ln of lines) {
    starts.push(pos);
    pos += ln.length + 1;
  }
  let inFence = false;
  let cut = -1;
  for (let i = 0; i < lines.length; i++) {
    const ln = lines[i] ?? '';
    if (FENCE.test(ln)) {
      inFence = !inFence;
      continue;
    }
    if (!inFence && ln.trim() === '' && i > 0) {
      cut = (starts[i] ?? 0) + ln.length + 1; // index just past this blank line
    }
  }
  // The trailing split element has no real "\n" after it → clamp the overshoot.
  return cut === -1 ? -1 : Math.min(cut, buf.length);
}
