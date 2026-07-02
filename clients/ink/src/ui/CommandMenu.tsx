/**
 * Slash-command suggestion list (MEM-16(2)) — shown under the InputBox while the buffer is a
 * slash-command prefix with matches. Borderless on purpose: full-width borders are the renderer's
 * resize-ghost source (see memory ink-renderer-resize), so this is plain indented rows. The
 * highlighted row (driven by the App's selection index) is the one Tab completes.
 */
import React from 'react';
import {Box, Text} from '../render/ink-compat.js';
import {ACCENT_HI, DIM, TEXT} from './theme.js';
import type {Command} from '../commands.js';
import {clampSel, menuWindow} from './menuModel.js';

export function CommandMenu({items, sel}: {items: readonly Command[]; sel: number}): React.ReactElement | null {
  if (items.length === 0) return null;
  const s = clampSel(sel, items.length);
  const {slice, offset} = menuWindow(items, s);
  const hidden = items.length - slice.length;
  const hint = (hidden > 0 ? `+${hidden} more · ` : '') + 'Tab complete · ↑↓ select · Esc close';
  return (
    <Box flexDirection="column" paddingLeft={2}>
      {slice.map((c, i) => {
        const selected = offset + i === s;
        // #937: an argument completion (subcommand/flag/choice) shows its bare token; a verb shows `/verb`
        const lhs = c.arg ? c.name : `/${c.name}${c.usage ? ' ' + c.usage : ''}`;
        return (
          <Box key={c.name} flexDirection="row">
            <Text color={selected ? ACCENT_HI : DIM}>{selected ? '› ' : '  '}</Text>
            <Text bold={selected} color={selected ? TEXT : DIM}>
              {lhs.padEnd(18)}
            </Text>
            <Text color={DIM}>{' ' + c.desc}</Text>
          </Box>
        );
      })}
      <Text color={DIM}>{'  ' + hint}</Text>
    </Box>
  );
}
