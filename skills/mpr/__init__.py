"""MPR — Multi-Perspective Reasoning, ironclad's public flagship example skill.

A reasoning-only orchestration skill that turns one hard, deliberative question into a panel of
distinct expert lenses, fans them out over ironclad's existing primitives (``parallel_reason`` /
``ReasoningWorkers.fanout`` + the P0 provider-router ``gx10._DISPATCHER``), and synthesises the
labelled results — *riding* the engine, never duplicating its dispatcher/store/governor.

This package ships under ``skills/mpr`` and consumes the public core surface without
shipping private literals. See ``README.md`` and ``../../docs/adr/0002-core-always-on-skills.md``.
"""
