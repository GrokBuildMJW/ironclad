"""ACE — Agentic Context Engineering (epic #855): the evolving-context **playbook** layer that extends the
#602 Loop-Intelligence reflection loop. Context is an itemized, sectioned **playbook** of **bullets**,
refined by Generator → Reflector → Curator with incremental delta updates + grow-and-refine.

ACE is the engine's **always-on** loop-intelligence core (wired in ACE-WIRE #863): the engine-side
PlaybookStore is registered unconditionally as the ``ack.lessons`` provider — there is **no enable flag** — and
it **supersedes** the #602 string-lesson + Process-SC consumers (with a one-time best-effort
EngineLessonStore→playbook migration). This package is pure / stdlib-only (imports nothing from the engine). Sub-modules are added
per cluster: ``playbook`` (the Bullet + Playbook data model, ACE-DATA #857) here; ``reflector`` (#858),
``curator`` (#859) etc. follow. Roles are namespaced ``ack.ace.*`` (distinct from the memory-service's
unrelated ``reflect_policy``/``curate``).
"""
from __future__ import annotations

from .playbook import (
    Bullet,
    Playbook,
    SCHEMA_VERSION,
    DEFAULT_SECTIONS,
    HELPFUL,
    HARMFUL,
    NEUTRAL,
)
from .reflector import (
    Trajectory,
    CandidateBullet,
    BulletRating,
    ReflectorOutput,
    reflect,
)
from .curator import (
    DeltaOp,
    Delta,
    curate,
    apply_delta,
    OP_ADD,
    OP_RATE,
    OP_TAG,
    OP_REMOVE,
)
from .grow import (
    dedupe,
    prune,
    refine,
    cosine,
    lexical_sim,
    DEFAULT_DEDUP_THRESHOLD,
)
from .generator import (
    GeneratorContext,
    select_relevant,
    prepare_context,
    to_trajectory,
    DEFAULT_TOP_K,
)
from .online import (
    AdaptConfig,
    adapt_once,
    OnlineAdapter,
    ReflectionWorker,
)
from .offline import (
    OfflineConfig,
    Sample,
    merge_deltas,
    build_offline,
    evaluate,
    warmup,
)
from .evaluation import (
    Outcome,
    accuracy,
    goal_completion,
    RolloutMeter,
    AdaptationCost,
    ace_adapt,
    full_rewrite_adapt,
    evolutionary_adapt,
    rollout_reduction,
    compare_adaptation,
    kv_cache_metrics,
    validate_epochs,
    EvalReport,
    DEFAULT_MAX_EPOCHS,
    ROLLOUT_REDUCTION_TARGET,
)
from .devtraj import (
    ledger_to_trajectories,
)
from .fork import (
    ForkSignal,
    ForkResolution,
    parse_fork_signal,
    parse_fork_resolution,
    fork_signals_from,
    fork_resolutions_from,
    FORK_SURFACE,
    FORK_RESOLVED_SURFACE,
)
from .robust import (
    adaptation_gain,
    quarantine_noisy,
    detect_contradictions,
    resolve_contradictions,
    unlearn,
    version_id,
    diff_versions,
    PlaybookHistory,
    DEFAULT_CONTRADICTION_OVERLAP,
)

__all__ = [
    "Bullet",
    "Playbook",
    "SCHEMA_VERSION",
    "DEFAULT_SECTIONS",
    "HELPFUL",
    "HARMFUL",
    "NEUTRAL",
    "Trajectory",
    "CandidateBullet",
    "BulletRating",
    "ReflectorOutput",
    "reflect",
    "DeltaOp",
    "Delta",
    "curate",
    "apply_delta",
    "OP_ADD",
    "OP_RATE",
    "OP_TAG",
    "OP_REMOVE",
    "dedupe",
    "prune",
    "refine",
    "cosine",
    "lexical_sim",
    "DEFAULT_DEDUP_THRESHOLD",
    "GeneratorContext",
    "select_relevant",
    "prepare_context",
    "to_trajectory",
    "DEFAULT_TOP_K",
    "AdaptConfig",
    "adapt_once",
    "OnlineAdapter",
    "ReflectionWorker",
    "OfflineConfig",
    "Sample",
    "merge_deltas",
    "build_offline",
    "evaluate",
    "warmup",
    "Outcome",
    "accuracy",
    "goal_completion",
    "RolloutMeter",
    "AdaptationCost",
    "ace_adapt",
    "full_rewrite_adapt",
    "evolutionary_adapt",
    "rollout_reduction",
    "compare_adaptation",
    "kv_cache_metrics",
    "validate_epochs",
    "EvalReport",
    "DEFAULT_MAX_EPOCHS",
    "ROLLOUT_REDUCTION_TARGET",
    "ledger_to_trajectories",
    "ForkSignal",
    "ForkResolution",
    "parse_fork_signal",
    "parse_fork_resolution",
    "fork_signals_from",
    "fork_resolutions_from",
    "FORK_SURFACE",
    "FORK_RESOLVED_SURFACE",
    "adaptation_gain",
    "quarantine_noisy",
    "detect_contradictions",
    "resolve_contradictions",
    "unlearn",
    "version_id",
    "diff_versions",
    "PlaybookHistory",
    "DEFAULT_CONTRADICTION_OVERLAP",
]
