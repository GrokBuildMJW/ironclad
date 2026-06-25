/**
 * Input affordance — top + bottom rules (open sides, like Claude Code / cli.py), a "> "
 * prompt and the live buffer with a caret. Implemented as a Box with only top/bottom
 * borders so it spans the width via Yoga (no manual rule-width math).
 */
import React from 'react';
import {Box, Text} from '../render/ink-compat.js';
import {SUBTLE, TEXT} from './theme.js';
import {displayBuffer} from './pasteStore.js';

export function InputBox({
  buffer,
  caret = true,
  hint = 'Frag etwas …',
}: {
  buffer: string;
  caret?: boolean;
  hint?: string;
}): React.ReactElement {
  // Multi-line PASTES live in the buffer as out-of-band sentinel tokens; `displayBuffer` renders each as a
  // friendly `[Pasted #N +L lines]` and compacts any residual typed newline to a ⏎ glyph so the box never
  // grows a row per line (LOK-5, #438). The real buffer is untouched — display-only.
  const display = displayBuffer(buffer);
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
