"""MPR — Multi-Perspective Reasoning (ironclad plugin, PRIVATE — not part of the OSS core export).

A reasoning-only orchestration skill that turns one hard, deliberative question into a panel of
distinct expert lenses, fans them out over ironclad's existing primitives (``parallel_reason`` /
``ReasoningWorkers.fanout`` + the P0 provider-router ``gx10._DISPATCHER``), and synthesises the
labelled results — *riding* the engine, never duplicating its dispatcher/store/governor.

This package lives under ``skills/`` (outside ``core/``) on purpose: it consumes the public
core surface but ships no private literals into it. See ``vault/Plan/mpr/`` for the specs.
"""
