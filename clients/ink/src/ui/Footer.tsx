/**
 * Pinned status footer — model · conn · watch · auto · tasks · perf. Mirrors cli.py's
 * _footer() content with the same palette.
 */
import React from 'react';
import {Box, Text} from '../render/ink-compat.js';
import type {StatusState} from './useStatusPoller.js';
import {ACCENT, DIM, ERROR, MODEL_BLUE, SUBTLE, SUCCESS} from './theme.js';

function Dot({on}: {on: boolean}): React.ReactElement {
  return <Text color={on ? SUCCESS : DIM}>{on ? '●' : '○'}</Text>;
}

function Sep(): React.ReactElement {
  return <Text color={SUBTLE}> · </Text>;
}

export function Footer({st}: {st: StatusState}): React.ReactElement {
  return (
    <Box>
      <Text bold color={ACCENT}>
        ◆ Ironclad
      </Text>
      <Sep />
      <Text color={DIM}>model </Text>
      <Text color={MODEL_BLUE}>{st.model}</Text>
      <Sep />
      <Text color={st.connected ? SUCCESS : ERROR}>{st.connected ? '●' : '○'}</Text>
      <Text color={DIM}> conn</Text>
      <Sep />
      <Dot on={st.watcher} />
      <Text color={DIM}> watch </Text>
      <Dot on={st.autopilot} />
      <Text color={DIM}> auto</Text>
      <Sep />
      <Text color={DIM}>{`${st.pending}P/${st.inProgress}IP/${st.done}D`}</Text>
      {st.perf ? (
        <>
          <Sep />
          <Text color={DIM}>{st.perf}</Text>
        </>
      ) : null}
    </Box>
  );
}
