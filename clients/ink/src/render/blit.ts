/**
 * blit — copy unchanged subtrees cell-for-cell between Surfaces (R4, pattern §4).
 *
 * Re-painting a subtree is expensive (Yoga reads, text wrapping, style interning). When a
 * subtree did not change, the renderer instead **blits** its cells straight from the front
 * buffer into the back buffer — a tight typed-array copy. The output diff then sees those
 * cells as equal to the front and emits nothing for them.
 *
 * Wide-glyph safety: a 2-column glyph is a lead cell (WIDE) + a continuation (WIDE_CONT). Three
 * ways a copy can orphan half a pair — `blitRect` guards all of them so it is sound for any caller:
 *  1. SOURCE split: the rect edge falls between a source lead/cont → widen the row span to whole
 *     source glyphs (so a pair is copied together).
 *  2. SOURCE lead with no room in `to` (narrowing blit, `to` narrower than `from`): the lead would
 *     land on the destination's last column with nowhere for its cont → write a blank instead of an
 *     orphan lead (else `flush` advances the cursor 2 cols for a 1-col cell and the row desyncs).
 *  3. DESTINATION split: the copy overwrites one half of a wide pair ALREADY in `to` whose partner
 *     lies just outside the span → blank that partner. Without this it stays outside the damage box,
 *     diff() never inspects it, and the back buffer (next front) diverges from the real terminal — a
 *     latent ghost on a later frame. Blanking via setCell pulls it into damage so diff reconciles it.
 */
import {Surface, WIDE, WIDE_CONT} from './surface.js';
import type {GeomCache, Rect} from './geomcache.js';
import type {VNode} from './vnode.js';

/**
 * Copy the cells of `rect` from `from` into `to`. Clamps to both surfaces' bounds and keeps every
 * wide-glyph pair whole at both the source and the destination (see the three guards above).
 * Touched cells are marked damaged on `to` (so a caller that reset damage gets an accurate window).
 */
export function blitRect(from: Surface, to: Surface, rect: Rect): void {
  const fw = from.width;
  const tw = to.width;
  const y0 = Math.max(0, rect.y);
  const y1 = Math.min(rect.y + rect.h, from.height, to.height);
  const baseX0 = Math.max(0, rect.x);
  const baseX1 = Math.min(rect.x + rect.w, fw, tw);
  if (baseX1 <= baseX0) return;

  for (let y = y0; y < y1; y++) {
    const row = y * fw;
    let lo = baseX0;
    let hi = baseX1;
    // (1) widen left: if we'd start on a source continuation, include its lead
    if (lo > 0 && ((from.flags[row + lo] ?? 0) & WIDE_CONT)) lo -= 1;
    // (1) widen right: if the last source cell is a lead, include its continuation (if room in `to`)
    if (hi < fw && hi <= tw - 1 && ((from.flags[row + (hi - 1)] ?? 0) & WIDE)) hi += 1;

    for (let x = lo; x < hi; x++) {
      const i = row + x;
      let cp = from.code[i] ?? 32;
      let fl = from.flags[i] ?? 0;
      const st = from.style[i] ?? 0;
      // (2) a source lead whose continuation cannot be copied (no room) → blank, never an orphan lead
      if (fl & WIDE && x + 1 >= hi) {
        cp = 32;
        fl = 0;
      }
      to.setCell(x, y, cp, st, fl);
    }

    // (3) repair destination half-glyphs the copy severed at the span boundaries (partner outside [lo,hi))
    if (lo > 0 && (to.getFlag(lo - 1, y) & WIDE)) to.setCell(lo - 1, y, 32, 0, 0);
    if (hi < tw && (to.getFlag(hi, y) & WIDE_CONT)) to.setCell(hi, y, 32, 0, 0);
  }
}

/**
 * Blit a node's cached rectangle from `from` to `to`. Returns false (and copies nothing) when
 * the node has no cached geometry or is contaminated — the caller must repaint it instead.
 */
export function blitNode(from: Surface, to: Surface, cache: GeomCache, node: VNode): boolean {
  if (cache.isContaminated(node)) return false;
  const rect = cache.get(node);
  if (!rect || rect.w <= 0 || rect.h <= 0) return false;
  blitRect(from, to, rect);
  return true;
}
