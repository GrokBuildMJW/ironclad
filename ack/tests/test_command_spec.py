"""Command-spec (#929/#930, epic #927) — the hand-authored parallel command description.

Pure tests of the spec DATA (no scripts/ dependency, so this runs in the export/clean-room tree too);
the live spec↔dispatch parity is tested separately in test_command_spec_parity.py (#940).
"""
from __future__ import annotations

import sys
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import command_spec as cs


def test_spec_is_internally_consistent():
    assert cs.validate() == []


def test_every_verb_has_a_valid_tier():
    assert cs.COMMAND_SPECS, "spec is empty"
    for c in cs.COMMAND_SPECS:
        assert c.tier in cs.VERB_TIERS, f"{c.verb!r} has invalid tier {c.tier!r}"
        assert c.summary.strip(), f"{c.verb!r} has no summary"


def test_config_get_and_set_are_distinct_verbs():
    # they are separate _dispatch branches; discovery + tiering must treat them apart
    assert cs.by_verb("config get") is not None and cs.by_verb("config set") is not None
    assert cs.by_verb("config get").tier == cs.READ_ONLY
    assert cs.by_verb("config set").tier == cs.MUTATING


def test_worst_offender_tiers_are_pinned():
    # #930 review follow-up: per-verb fixtures for the known destructive/costly verbs so a mis-tiering
    # (which the presence-only parity guard cannot catch) is caught here.
    assert cs.by_verb("project").tier == cs.DESTRUCTIVE      # has delete --purge
    assert cs.by_verb("autoplan").tier == cs.COSTLY          # uncapped model loop (RED warning)
    assert cs.by_verb("ace").tier == cs.COSTLY               # eval/warmup call the model
    assert cs.by_verb("design").tier == cs.COSTLY            # spends a model turn for proposals
    assert cs.by_verb("generate").tier == cs.COSTLY          # scaffolds files
    assert cs.by_verb("tool").tier == cs.COSTLY              # runs an arbitrary tool
    assert cs.by_verb("help").tier == cs.READ_ONLY
    assert cs.by_verb("status").tier == cs.READ_ONLY


def test_config_set_declares_the_six_boot_only_keys():
    c = cs.by_verb("config set")
    assert set(c.boot_only_keys) == {
        "setup.type", "security.profile", "security.web_in_sealed",
        "search.enabled", "search.adapter", "search.api_key_env",
    }
    assert cs.SPEC_FROZEN_CONFIG_KEYS == set(c.boot_only_keys)


def test_boot_only_keys_only_on_config_set():
    for c in cs.COMMAND_SPECS:
        if c.verb != "config set":
            assert not c.boot_only_keys, f"{c.verb!r} must not declare boot_only_keys"


def test_generate_declares_its_required_flags():
    flags = {f.name for f in cs.by_verb("generate").flags}
    assert {"--domain", "--case", "--description"} <= flags


def test_catalogue_entries_serialize_every_verb():
    # #931: the serialized form served via /catalogue for client-side command generation.
    # #1264: deprecated verbs are excluded from the advertised catalogue, so the count is the non-deprecated
    # verbs — not len(COMMAND_SPECS).
    entries = cs.catalogue_entries()
    assert len(entries) == len([c for c in cs.COMMAND_SPECS if not cs.is_deprecated(c)])
    for e in entries:
        assert set(e) == {"name", "tier", "usage", "summary", "subcommands", "flags"}   # #936: + structured
        assert e["tier"] in cs.VERB_TIERS and e["summary"]
        for f in e["flags"]:                                     # #936: structured flags for guided-input/autocomplete
            assert set(f) == {"name", "required", "choices", "summary"}
    lc = next(e for e in entries if e["name"] == "lifecycle")
    assert "gate" in lc["usage"] and "--tree" in lc["usage"]     # subcommands + flags render into usage
    assert "gate" in lc["subcommands"] and any(f["name"] == "--tree" for f in lc["flags"])


def test_catalogue_excludes_deprecated_but_keeps_dispatchable():
    # #1264: a deprecated alias (e.g. /initiative) must NOT be advertised via /catalogue (nor autocomplete),
    # yet stays a real, dispatchable spec verb (the entry + its dispatch branch remain for back-compat).
    names = {e["name"] for e in cs.catalogue_entries()}
    assert "initiative" not in names                        # not advertised
    assert cs.by_verb("initiative") is not None             # still a real spec verb (dispatchable)
    assert cs.is_deprecated(cs.by_verb("initiative"))       # classified deprecated → drives the exclusion
    assert "project" in names                               # the canonical verb IS advertised


def test_guided_usage_is_spec_derived():
    # #936: a single-source usage/guidance line — subcommands + flags come from the spec, not a hand string
    u = cs.guided_usage("lifecycle")
    assert u.startswith("usage: /lifecycle") and "gate" in u and "--tree" in u
    assert cs.guided_usage("nonesuch") == ""                      # unknown verb → empty (caller falls back)
    # a required flag is bare, an optional flag is [bracketed]
    g = cs.guided_usage("generate")
    assert "--domain" in g and "[--" not in g.split("--domain")[0]  # generate's flags are required → not bracketed


def test_resolve_command_exact_alias_prefix_suggest():
    # #934: the deterministic, zero-cost resolution SSOT
    known = {"config", "config get", "config keys", "lifecycle", "project", "status", "help"}
    A, unsafe = cs.ALIASES, cs.unsafe_first_words()
    assert cs.resolve_command("config", known, A, unsafe) == ("exact", "config")
    assert cs.resolve_command("lg", known, A, unsafe) == ("alias", "lifecycle gate")
    assert cs.resolve_command("stat", known, A, unsafe) == ("prefix", "status")     # unique + safe
    assert cs.resolve_command("confog", known, A, unsafe)[0] == "suggest"           # typo → did-you-mean
    assert cs.resolve_command("proj", known, A, unsafe) == ("suggest", "project")   # destructive prefix → suggest
    assert cs.resolve_command("zzzzzz", known, A, unsafe) == ("unknown", "")         # nothing close


def test_unsafe_first_words_are_destructive_or_costly():
    u = cs.unsafe_first_words()
    assert {"project", "autoplan", "ace", "generate", "tool"} <= u                  # destructive/costly
    assert not ({"config", "help", "status", "lifecycle"} & u)                      # safe verbs never unsafe


def test_guided_usage_covers_the_worst_offenders():
    # #953: every flag-heavy worst-offender has an accurate spec-derived usage (single source) — the dispatch
    # usage returns now call guided_usage(verb), so this pins what the operator is guided with.
    assert "<dotted.key>" in cs.guided_usage("config set") and "<value>" in cs.guided_usage("config set")
    assert "warmup|eval" in cs.guided_usage("ace")
    assert cs.guided_usage("design").startswith("usage: /design --options [N]")
    assert "range 2..8, default 2" in cs.by_verb("design").flags[0].summary
    assert "new <name>" in cs.guided_usage("project") and "delete <id>" in cs.guided_usage("project")
    assert "--domain" in cs.guided_usage("generate")
    assert cs.by_verb("project").usage           # the override is the single source for the multi-form verb
    # render_usage (the /catalogue hint) honors the same override → no client↔dispatch drift
    assert cs.render_usage(cs.by_verb("project")) == cs.by_verb("project").usage


# ── #967: model-facing command-surface summary (injected into the orchestrator system context) ──────
def test_context_summary_marks_project_primary_and_initiative_deprecated():
    import command_spec as cs
    s = cs.context_summary()
    assert "/project" in s and "/initiative" in s
    dep = s.index("Deprecated")                       # the deprecated-section header
    assert s.index("/initiative") > dep               # /initiative is listed under it
    assert s.index("/project ") < dep                 # /project is canonical (before the deprecated section)
    assert "Deprecated alias for /project" in s        # derived from the spec's own summary, not hardcoded


def test_context_summary_names_the_destructive_verbs():
    import command_spec as cs
    s = cs.context_summary()
    assert "Destructive/costly" in s
    for u in cs.unsafe_first_words():
        assert f"/{u}" in s


def test_context_summary_is_empty_without_specs(monkeypatch):
    import command_spec as cs
    monkeypatch.setattr(cs, "COMMAND_SPECS", [])
    assert cs.context_summary() == ""                  # fail-soft: an empty spec injects nothing
