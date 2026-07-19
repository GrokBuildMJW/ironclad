/**
 * A foldable tool-call block (#1167, epic #1144) — Claude-Code parity.
 *
 * Collapsed by default: `● <label>  ▸ N lines` (or `…` while the tool is still running). Click anywhere on
 * it to expand to the full result under a `⎿` corner; click again to collapse. State persists across the
 * app's re-renders because the committed React element keeps its instance.
 */
import React, {useState} from 'react';
import {Box, Text} from '../render/ink-compat.js';
import {DIM, SUBTLE, ERROR} from './theme.js';
import {toolMeta} from './toolMeta.js';
import {shellLabel} from '../tools/shell.js';

/** #1196: does a tool result read as an error? Test the STRIPPED text for the `✗`/`ERROR` markers —
 *  a coloured line now keeps its SGR, so a colour-at-column-0 error line (`\x1b[31m✗ …`) would otherwise
 *  start with an escape byte and defeat a raw `startsWith` check. */
export function isErrorResult(result: string[]): boolean {
  return result.some((l) => {
    const s = l.replace(/\x1b\[[0-9;]*m/g, '');
    return s.startsWith('✗') || s.startsWith('ERROR');
  });
}

export function ToolCall({
  label,
  result,
  defaultOpen = false,
}: {
  label: string;
  result: string[];
  defaultOpen?: boolean;
}): React.ReactElement {
  const done = result.length > 0;
  const phase = `${label}:${done ? 'done' : 'running'}`;
  const [fold, setFold] = useState({phase, open: defaultOpen});
  // The result's first line is the existing completion signal. A phase mismatch closes an in-flight
  // inspection synchronously, so its completed summary replaces the full detail before the turn ends.
  const open = fold.phase === phase ? fold.open : false;
  const toggle = (): void => setFold({phase, open: !open});
  const isError = isErrorResult(result);
  const bodyColor = isError ? ERROR : SUBTLE;
  // Relabel a shell tool's header with the shell it actually runs in (per command: PowerShell cmdlets →
  // PowerShell, else Git Bash), so `● Bash(Get-ChildItem)` from the engine reads truthfully.
  const cmd = /^\w+\((.*)\)$/.exec(label)?.[1] ?? '';
  const meta = toolMeta(label, done, result.length, shellLabel(cmd));

  if (open) {
    // expanded: the exact `Kind(arg)` header + the full result under a `⎿` corner
    return (
      <Box flexDirection="column" onClick={toggle}>
        <Text color={DIM}>{`● ${meta.header}`}</Text>
        {result.map((ln, i) => (
          <Text key={i} color={bodyColor}>
            {(i === 0 ? '  ⎿ ' : '     ') + ln}
          </Text>
        ))}
      </Box>
    );
  }

  // collapsed: the action summary + a one-line detail (`$ <cmd>` / `<path>`)
  return (
    <Box flexDirection="column" onClick={toggle}>
      <Text color={DIM}>{`● ${meta.summary}`}</Text>
      {meta.detail ? <Text color={isError ? ERROR : SUBTLE}>{`  ⎿ ${meta.detail}`}</Text> : null}
    </Box>
  );
}
