/**
 * Input affordance — top + bottom rules (open sides, like Claude Code / cli.py), a "> "
 * prompt and the live buffer with a caret. Implemented as a Box with only top/bottom
 * borders so it spans the width via Yoga (no manual rule-width math).
 */
import React from 'react';
import {Box, Text} from '../render/ink-compat.js';
import {SUBTLE, TEXT} from './theme.js';

export function InputBox({
  buffer,
  caret = true,
  hint = 'Frag etwas …',
}: {
  buffer: string;
  caret?: boolean;
  hint?: string;
}): React.ReactElement {
  // A multi-line input (especially a paste) is shown COMPACT on one logical line: each newline becomes a
  // ⏎ glyph so the box doesn't grow one row per line (LOK-5). The real buffer (with \n) is untouched and
  // is sent as a single turn — this is display-only.
  const display = buffer.includes('\n') ? buffer.replace(/\r?\n/g, ' ⏎ ') : buffer;
  return (
    <Box
      flexDirection="column"
      borderStyle="single"
      borderColor={SUBTLE}
      borderLeft={false}
      borderRight={false}
      paddingX={0}
    >
      <Box flexDirection="row">
        {/* this text ends exactly at the caret cell → the renderer places the real terminal cursor
            there (native blink + IME), so there's no glyph caret to depend on a font for */}
        <Text cursor={caret}>
          <Text color={SUBTLE}>{'> '}</Text>
          {buffer ? <Text color={TEXT}>{display}</Text> : null}
        </Text>
        {!buffer ? <Text color={SUBTLE}>{hint}</Text> : null}
      </Box>
    </Box>
  );
}
