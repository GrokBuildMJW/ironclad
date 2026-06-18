/**
 * The "working" line shown while a turn streams — animated spinner + verb + elapsed +
 * token count + interrupt hint. Mirrors cli.py's _working_line().
 */
import React from 'react';
import {Text} from '../render/ink-compat.js';
import {ACCENT, DIM, SPIN_FRAMES} from './theme.js';

export function WorkingLine({
  verb,
  frame,
  seconds,
  tokens,
}: {
  verb: string;
  frame: number;
  seconds: number;
  tokens: number;
}): React.ReactElement {
  const f = SPIN_FRAMES[frame % SPIN_FRAMES.length];
  const tok = tokens >= 1000 ? `${(tokens / 1000).toFixed(1)}k` : String(tokens);
  return (
    <Text color={ACCENT}>
      {` ${f} ${verb}… `}
      <Text color={DIM}>{`(${seconds}s · ↑ ${tok} tokens · esc to interrupt)`}</Text>
    </Text>
  );
}
