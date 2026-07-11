"""Command-spec — a HAND-AUTHORED, machine-readable description of the slash-command surface.

Epic #927 (user-friendliness), C0 foundation (#929/#930). This is a **parallel description** of the
engine's command surface: it does NOT drive dispatch and `engine.gx10._dispatch` is never refactored to
read from it (the fail-closed executor the machine-gated dev-loop types verbatim stays untouched — C0
decision #9). The spec exists so the friendly-UX layers can be *derived* from ONE source:

  * client-side discovery + autocomplete (ink COMMANDS server-subset + ``/catalogue``, #931),
  * a server-side guided-input / confirm contract keyed on the per-command danger-tier (#935/#936),
  * a deterministic alias / did-you-mean net (#934).

Because a hand-authored parallel list drifts (lifecycle/fork/ace already fell out of both client
registries before this epic), the ONLY thing that keeps it honest is the ``spec ↔ dispatch`` parity guard
(#940, ``scripts/ci/check_command_spec_parity.py``): it derives the verb set from ``_dispatch`` *source*,
imports the real ``_FROZEN_CONFIG_KEYS``, and introspects ``ack.generator.build_parser`` — asserting this
spec matches all three. Keep this module in sync with ``_dispatch``; the guard fails the build otherwise.

This module is PURE DATA (no ``gx10`` / ``generator`` import) so it stays import-light and boundary-clean;
every live cross-check lives in the guard.

Danger tiers (#930), authoritative + never model-graded — a verb's tier is the MAX danger of its forms:
  * ``read_only``  — no state change (help, status, discovery, config get, context, ls, cat, fork view).
  * ``mutating``   — changes engine/session/project state (config set, watcher/autopilot toggles, project…).
  * ``destructive``— can irreversibly delete work (project delete --purge).
  * ``costly``     — spends model turns / spawns work (autoplan model loop, ace eval, tool, generate).
  * ``boot_only``  — a config KEY that a runtime ``/config set`` must refuse (tracked per-key, not per-verb).
Tier VALUES are hand-assigned; the guard asserts a tier is PRESENT + valid, not that it is the *correct*
one (see the per-verb fixtures in the tests for the known destructive/costly verbs).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

# ── danger tiers (#930) ───────────────────────────────────────────────────────────────────────────
READ_ONLY = "read_only"
MUTATING = "mutating"
DESTRUCTIVE = "destructive"
COSTLY = "costly"
BOOT_ONLY = "boot_only"
#: verb-level tiers (boot_only is a config-KEY property, not a verb tier).
VERB_TIERS = frozenset({READ_ONLY, MUTATING, DESTRUCTIVE, COSTLY})


@dataclass(frozen=True)
class FlagSpec:
    """One flag/option of a command. ``choices``/``required`` mirror the hand-written parser (for the
    families) or ``ack.generator.build_parser`` (for ``generate``, guard-verified)."""
    name: str                                   # e.g. "--tree", "--type", "on|off" for toggles
    summary: str = ""
    required: bool = False
    choices: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CommandSpec:
    """One canonical dispatch verb (exactly as it appears as a ``_dispatch`` branch literal — e.g.
    ``config get`` and ``config set`` are separate verbs, while ``ace``/``project``/``lifecycle`` are
    single family verbs whose sub-verbs live in ``subcommands``)."""
    verb: str
    tier: str
    summary: str
    subcommands: Tuple[str, ...] = ()
    flags: Tuple[FlagSpec, ...] = ()
    boot_only_keys: Tuple[str, ...] = ()        # config set only: keys a runtime set must refuse (#932)
    usage: str = ""                             # #953: optional hand-authored usage form (the part AFTER
                                                # "/<verb> ") for a family verb whose per-subcommand args
                                                # a flat subcommands+flags render cannot capture (e.g.
                                                # project's `new <name>` / `delete <id>`); else auto-rendered.


_ON_OFF = (FlagSpec("on|off", "toggle (bare = show state)", choices=("on", "off")),)

# ── the hand-authored spec — one entry per _dispatch server-verb branch (gx10.py ~6602-6803) ─────────
# NOTE: verbs MUST equal the _dispatch branch literals; the #940 guard derives them from source and fails
# on any drift. The dynamic prompt-name branch (6804) and the `else`/agent.run fall-through (6810) are
# NOT verbs and are excluded by the guard.
COMMAND_SPECS: Tuple[CommandSpec, ...] = (
    CommandSpec("help", READ_ONLY, "Show the grouped command help."),
    CommandSpec("clear", MUTATING, "Clear the conversation context for this session."),
    CommandSpec("status", READ_ONLY, "Show model/session/perf status."),
    CommandSpec("prompts", READ_ONLY, "List the built-in prompt items."),
    CommandSpec("skills", READ_ONLY, "List the available skills/tools."),
    CommandSpec("config", READ_ONLY, "Show the effective runtime config."),
    CommandSpec("config get", READ_ONLY, "Read one config value by dotted key.",
                flags=(FlagSpec("<dotted.key>", "e.g. context.rag_enabled", required=True),)),
    CommandSpec("config keys", READ_ONLY, "List the settable config keys (boot-only keys flagged)."),
    CommandSpec("config set", MUTATING, "Set one runtime config value by dotted key.",
                flags=(FlagSpec("<dotted.key>", required=True),
                       FlagSpec("<value>", "coerced: on/off→bool, int, float, else str", required=True)),
                boot_only_keys=("setup.type", "security.profile", "security.web_in_sealed",
                                "search.enabled", "search.adapter", "search.api_key_env")),
    CommandSpec("read", MUTATING, "Read a file INTO the model context.",
                flags=(FlagSpec("<file>", required=True),)),
    CommandSpec("write", MUTATING, "Write the last model reply to a path.",
                flags=(FlagSpec("<path>", required=True),)),
    CommandSpec("cat", READ_ONLY, "Display a file (does not load it into context).",
                flags=(FlagSpec("<path>", required=True),)),
    CommandSpec("ls", READ_ONLY, "List a directory (default: the project workdir).",
                flags=(FlagSpec("[dir]",),)),
    CommandSpec("auto", COSTLY,
                "Automation meta-switch: on = FULL automation (watcher + autopilot + continuation, "
                "every unit = a paid coder run), off = guided mode (engine recommends, operator drives).",
                flags=(FlagSpec("on [N] | off", "N caps the task count; on with no N runs unbounded",
                                choices=("on", "off")),)),
    CommandSpec("watcher", MUTATING, "Deprecated compatibility alias for /auto on|off.", flags=_ON_OFF),
    CommandSpec("autopilot", MUTATING, "Toggle autopilot (auto-launch of agents).", flags=_ON_OFF),
    CommandSpec("autoplan", COSTLY, "Toggle autoplan — a model-driven planning loop (spends tokens).",
                flags=(FlagSpec("on [N] | off", "N caps the task count; on with no N loops uncapped",
                                choices=("on", "off")),)),
    CommandSpec("log-terminal", MUTATING, "Toggle the live-log window for the next autopilot start.",
                flags=_ON_OFF),
    CommandSpec("rag", MUTATING, "Toggle per-turn retrieval (RAG) for this session.", flags=_ON_OFF),
    CommandSpec("context", READ_ONLY, "Show the memory/context diagnosis (summary + last retrieval)."),
    CommandSpec("initiative", MUTATING, "Deprecated alias for /project (kept one release).",
                subcommands=("new", "list", "use", "active", "reconcile")),
    CommandSpec("switch", MUTATING, "Switch the active project.",
                flags=(FlagSpec("<project_id>", "from /project list", required=True),)),
    CommandSpec("approve", MUTATING,
                "Approve a design (bare /approve or /approve design [slug]) or promote constraints "
                "(/approve constraint <id|all> [--slug]). Pending constraint forks block design approval.",
                subcommands=("design", "constraint"),
                flags=(FlagSpec("[slug]", "design: the unit to approve (default: active)", required=False),
                       FlagSpec("<id|all>", "constraint: language|network|all", required=False),
                       FlagSpec("--slug", "constraint: unit slug (default: active)"),)),
    CommandSpec("dismiss", MUTATING,
                "Dismiss a suggested typed constraint (drop a model guess so it stops gating).",
                subcommands=("constraint",),
                flags=(FlagSpec("<id|all>", "constraint: language|network|all", required=True),
                       FlagSpec("--slug", "constraint: unit slug (default: active)"),)),
    CommandSpec("board", MUTATING,
                "Render the task board (all units grouped pending/in_progress/done) to BOARD.md and show it (S6).",
                flags=(FlagSpec("[slug]", "the unit to board (default: active)"),)),
    CommandSpec("lifecycle", MUTATING, "Run the DELIVER-leg lifecycle-completeness gate.",
                subcommands=("gate",),
                flags=(FlagSpec("--slug", "default: active project slug"),
                       FlagSpec("--tree", "delivery tree sha (default: resolved from git HEAD, #933)"),
                       FlagSpec("--ledger", "default: <root>/.devloop/ledger.jsonl"),
                       FlagSpec("--stages", "csv; default: delivery"))),
    CommandSpec("fork", MUTATING,
                "List pending constraint-fork envelopes / MPR proposals, or resolve one "
                "(/fork decide <fork-id> --choice keep|counter). Opaque fork ids (no #N).",
                subcommands=("list", "decide"),
                flags=(FlagSpec("[fork-id|unit]", "list one envelope or M5 unit proposal"),
                       FlagSpec("--choice", "decide: keep|counter"),)),
    CommandSpec("ace", COSTLY, "ACE playbook ops: warmup/eval (model) + snapshot/versions/rollback/unlearn (local safety net).",
                subcommands=("warmup", "eval", "snapshot", "versions", "rollback", "unlearn"),
                flags=(FlagSpec("--ledger", "dev-loop ledger path (defaults to <root>/.devloop/ledger.jsonl)"),)),
    CommandSpec("project", DESTRUCTIVE, "Project/workspace management (delete --purge is destructive).",
                subcommands=("new", "list", "use", "active", "track", "delete", "archive", "unarchive"),
                flags=(FlagSpec("--path",), FlagSpec("--purge", "delete: irreversibly remove files")),
                usage="list [--all] | new <name> [--path <dir>] | active | "
                      "track new|use|list | delete <id> [--purge] | archive|unarchive <id>"),
    CommandSpec("generate", COSTLY, "Scaffold a new case/prompt (writes files).",
                flags=(FlagSpec("--domain", required=True), FlagSpec("--case", required=True),
                       FlagSpec("--description", required=True),
                       FlagSpec("--kind", choices=("case", "prompt")),
                       FlagSpec("--phase", choices=("MVP", "V1", "V2", "V3", "out-of-scope")),
                       FlagSpec("--tier", choices=("high", "medium", "low")))),
    CommandSpec("tool", COSTLY, "Run a tool directly by name (may spend a model turn).",
                flags=(FlagSpec("<name>", required=True),
                       FlagSpec("<json-args | text>", "text maps to the first required parameter"))),
)

#: the boot-only config keys the spec declares (guard asserts == gx10._FROZEN_CONFIG_KEYS, #940b).
SPEC_FROZEN_CONFIG_KEYS = frozenset(next(c for c in COMMAND_SPECS if c.verb == "config set").boot_only_keys)


def verbs() -> "frozenset[str]":
    """The set of canonical verb strings in the spec (must equal the _dispatch server-verb literals)."""
    return frozenset(c.verb for c in COMMAND_SPECS)


def by_verb(verb: str) -> Optional[CommandSpec]:
    for c in COMMAND_SPECS:
        if c.verb == verb:
            return c
    return None


# #934: short aliases for the long/common commands (alias -> canonical command string). English-only; the
# single source both clients read (the ink client via GET /catalogue, the Python client by import).
ALIASES: "dict[str, str]" = {
    "lg": "lifecycle gate", "cfg": "config", "keys": "config keys",
    "cfgget": "config get", "cfgset": "config set", "pj": "project", "gen": "generate",
}


def _edit_distance(a: str, b: str) -> int:
    """Bounded, deterministic Levenshtein for did-you-mean (no deps)."""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (0 if ca == cb else 1)))
        prev = cur
    return prev[-1]


def resolve_command(token: str, known_verbs, aliases=None, unsafe=frozenset()) -> "tuple[str, str]":
    """#934: deterministic, ZERO-COST resolution of a leading command token (no model). Returns
    ``(kind, value)``:
      * ``exact``  — *token* is a known command first-word (value = token);
      * ``alias``  — value = the canonical expansion (e.g. ``lg`` -> ``lifecycle gate``);
      * ``prefix`` — value = the single known first-word that starts with *token*, IFF it is not ``unsafe``
                     (a destructive/costly verb never auto-resolves from a prefix — it only suggests);
      * ``suggest``— value = the nearest known first-word (edit-distance ≤ 2, or an unsafe prefix hit) — a
                     SUGGESTION, never auto-run;
      * ``unknown``— value = "" (nothing close).
    ``known_verbs`` = the known command first-words (server spec + client-local); ``unsafe`` = the first-words
    that must be confirmed rather than prefix-auto-resolved (default none)."""
    aliases = aliases or {}
    t = (token or "").strip().lower()
    if not t:
        return ("unknown", "")
    if t in aliases:
        return ("alias", aliases[t])
    firsts = {v.split()[0] for v in known_verbs}
    if t in firsts:
        return ("exact", t)
    pref = sorted(f for f in firsts if f.startswith(t))
    if len(pref) == 1:
        return ("suggest", pref[0]) if pref[0] in unsafe else ("prefix", pref[0])
    cand = sorted(firsts, key=lambda f: (_edit_distance(t, f), f))
    if cand and _edit_distance(t, cand[0]) <= 2:
        return ("suggest", cand[0])
    return ("unknown", "")


def unsafe_first_words() -> "frozenset[str]":
    """The command first-words whose tier is destructive/costly — never auto-resolved from a bare prefix
    (#934); a prefix that lands on one only *suggests* it. Derived from the spec (single source)."""
    return frozenset(c.verb.split()[0] for c in COMMAND_SPECS if c.tier in (DESTRUCTIVE, COSTLY))


def _usage_hint(c: CommandSpec) -> str:
    """The usage form AFTER ``/<verb>`` — the hand-authored ``usage`` override if set (#953), else rendered
    from subcommands + flags (required bare, optional ``[bracketed]``, choices ``{a|b}``). Single source for
    both the ``/catalogue`` hint (render_usage) and the dispatch usage line (guided_usage)."""
    if c.usage:
        return c.usage
    parts: list[str] = []
    if c.subcommands:
        parts.append("|".join(c.subcommands))
    for f in c.flags:
        seg = f"{f.name} {{{'|'.join(f.choices)}}}" if f.choices else f.name
        parts.append(seg if f.required else f"[{seg}]")
    return " ".join(parts)


def render_usage(c: CommandSpec) -> str:
    """A one-line usage hint from the spec (the part after ``/<verb>``), for the ``/catalogue`` entry."""
    return _usage_hint(c)


def is_deprecated(c: CommandSpec) -> bool:
    """A verb is *deprecated* iff its ``summary`` says so (one convention, single source). A deprecated verb
    stays fully dispatchable for back-compat, but is NOT advertised — neither in the model-facing
    :func:`context_summary` nor in the client-facing :func:`catalogue_entries` (so it disappears from
    ``/catalogue`` + slash-autocomplete). #1264."""
    return c.summary.lower().startswith("deprecated")


def catalogue_entries() -> "list[dict]":
    """The server-verb spec serialized for ``GET /catalogue`` + client generation (#931/#936):
    ``[{name, tier, usage, summary, subcommands, flags:[{name, required, choices, summary}]}]``. Pure — the
    ink server-command completions are generated FROM this (with the client's static list as the cold-start
    fallback), and the structured ``flags``/``subcommands`` back the client's guided-input + autocomplete
    (#936/#937). **Deprecated verbs are excluded** (#1264): they stay dispatchable but must not be
    advertised in autocomplete."""
    return [{"name": c.verb, "tier": c.tier, "usage": render_usage(c), "summary": c.summary,
             "subcommands": list(c.subcommands),
             "flags": [{"name": f.name, "required": f.required, "choices": list(f.choices),
                        "summary": f.summary} for f in c.flags]}
            for c in COMMAND_SPECS if not is_deprecated(c)]


def guided_usage(verb: str) -> str:
    """#936: a spec-derived usage/guidance line for a command — ``usage: /<verb> <subs> <flag …>`` with
    required flags bare, optional flags ``[bracketed]``, and choices shown ``{a|b}`` — so an
    under-specified command guides the operator from ONE source, not a hand-written string. ``""`` if
    the verb is unknown."""
    c = by_verb(verb)
    if c is None:
        return ""
    hint = _usage_hint(c)
    line = "usage: /" + c.verb + (" " + hint if hint else "")
    return line + (f"  — {c.summary}" if c.summary else "")


def context_summary() -> str:
    """#967: a compact, model-facing digest of the command surface, injected into the orchestrator's
    system context so it names commands correctly and never recommends a deprecated one (the operator hit
    exactly this: the model pushed the deprecated ``/initiative`` and denied ``/project``). Derived from the
    spec — a verb is *deprecated* iff its ``summary`` says so, danger comes from ``unsafe_first_words`` — so
    it can never drift from the real dispatch. Returns ``""`` if the spec is empty (the caller injects
    nothing)."""
    if not COMMAND_SPECS:
        return ""
    canonical = [c for c in COMMAND_SPECS if not is_deprecated(c)]
    deprecated = [c for c in COMMAND_SPECS if is_deprecated(c)]
    lines = ["Slash-command surface (use these canonical command names EXACTLY — never invent, guess, or",
             "recommend a command that is not listed here):"]
    lines += [f"  /{c.verb} — {c.summary}" for c in canonical]
    if deprecated:
        lines.append("Deprecated — do NOT recommend these; use the canonical command each points to:")
        lines += [f"  /{c.verb} — {c.summary}" for c in deprecated]
    unsafe = sorted(unsafe_first_words())
    if unsafe:
        lines.append("Destructive/costly — confirm the operator's intent before invoking: "
                     + ", ".join("/" + u for u in unsafe) + ".")
    return "\n".join(lines)


def validate() -> "list[str]":
    """Internal consistency (no live deps): unique verbs, every tier valid, boot-only keys only on
    ``config set``. Returns a list of problems (empty = ok). The live spec↔dispatch parity is #940."""
    problems: list[str] = []
    seen: set[str] = set()
    for c in COMMAND_SPECS:
        if c.verb in seen:
            problems.append(f"duplicate verb {c.verb!r}")
        seen.add(c.verb)
        if c.tier not in VERB_TIERS:
            problems.append(f"{c.verb!r}: invalid tier {c.tier!r} (not in {sorted(VERB_TIERS)})")
        if c.boot_only_keys and c.verb != "config set":
            problems.append(f"{c.verb!r}: boot_only_keys only allowed on 'config set'")
    return problems
