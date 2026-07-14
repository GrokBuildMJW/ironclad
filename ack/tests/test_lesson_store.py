"""Project-private lesson distiller — provider tests (epic #602 SUB-5).

Proves, offline (no model, no live store):

  * the concrete `EngineLessonStore` satisfies the `ack.lessons.LessonProvider` protocol and supplies the
    real semantics the #601 seam delegates to — scope-isolated persistence, recency + query ranking,
    typed categories, compaction, a scope-priority `brief`, and the optional `forget` purge;
  * it is robust (a corrupt / missing scope file reads as empty; weird opaque scopes get safe filenames);
  * the retired `lessons.enabled` / `lessons.max_per_scope` inputs warn and cannot rewire the always-on ACE
    `PlaybookStore`; `EngineLessonStore` remains covered as a persistence backend and migration source.

    python -m pytest ack/tests/test_lesson_store.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ack.lessons import LessonProvider

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

from lesson_store import EngineLessonStore, LessonCategory  # noqa: E402

# a realistic opaque scope (the engine's mem_scope = "<mem_ns>::track::<tid>")
SCOPE = "proj_ab12cd34::track::feature-x"
SCOPE_B = "proj_zz99::track::main"


def _store(tmp_path, **kw) -> EngineLessonStore:
    return EngineLessonStore(tmp_path / "lessons", **kw)


# ─── protocol + round-trip + isolation ────────────────────────────────────────────────────────────
def test_satisfies_lessonprovider_protocol(tmp_path):
    assert isinstance(_store(tmp_path), LessonProvider)


def test_report_then_get_roundtrip(tmp_path):
    s = _store(tmp_path)
    s.report_lesson(SCOPE, "prefer rg over grep")
    assert s.get_lessons(SCOPE) == ["prefer rg over grep"]


def test_scopes_are_isolated(tmp_path):
    s = _store(tmp_path)
    s.report_lesson(SCOPE, "lesson A")
    s.report_lesson(SCOPE_B, "lesson B")
    assert s.get_lessons(SCOPE) == ["lesson A"]
    assert s.get_lessons(SCOPE_B) == ["lesson B"]


def test_recency_order_newest_first(tmp_path):
    s = _store(tmp_path)
    for t in ("first", "second", "third"):
        s.report_lesson(SCOPE, t)
    assert s.get_lessons(SCOPE) == ["third", "second", "first"]


def test_get_respects_limit(tmp_path):
    s = _store(tmp_path)
    for i in range(5):
        s.report_lesson(SCOPE, f"l{i}")
    assert s.get_lessons(SCOPE, limit=2) == ["l4", "l3"]


# ─── query ranking ────────────────────────────────────────────────────────────────────────────────
def test_query_ranks_by_term_overlap(tmp_path):
    s = _store(tmp_path)
    s.report_lesson(SCOPE, "use the warm cache for retrieval")
    s.report_lesson(SCOPE, "totally unrelated note")
    s.report_lesson(SCOPE, "retrieval needs a warm cache hit")
    ranked = s.get_lessons(SCOPE, query="warm cache retrieval", limit=2)
    assert "totally unrelated note" not in ranked
    assert all("cache" in r or "retrieval" in r for r in ranked)


# ─── compaction ───────────────────────────────────────────────────────────────────────────────────
def test_compaction_drops_oldest_over_cap(tmp_path):
    s = _store(tmp_path, max_per_scope=3)
    for i in range(6):
        s.report_lesson(SCOPE, f"l{i}")
    got = s.get_lessons(SCOPE, limit=99)
    assert got == ["l5", "l4", "l3"]              # only the 3 newest survive
    assert len(got) == 3


def test_exact_duplicate_is_refreshed_not_doubled(tmp_path):
    s = _store(tmp_path)
    s.report_lesson(SCOPE, "dup")
    s.report_lesson(SCOPE, "other")
    s.report_lesson(SCOPE, "dup")                 # same text+category → refresh recency, no new row
    got = s.get_lessons(SCOPE, limit=99)
    assert got == ["dup", "other"]               # one 'dup', and it is now the most recent


# ─── typed categories ─────────────────────────────────────────────────────────────────────────────
def test_record_and_read_by_category(tmp_path):
    s = _store(tmp_path)
    s.record(SCOPE, "missing repo context last time", LessonCategory.LAST_FAILURE_REASON)
    s.record(SCOPE, "stage tests before deliver", LessonCategory.BEST_KNOWN_PATH)
    assert s.by_category(SCOPE, LessonCategory.LAST_FAILURE_REASON) == ["missing repo context last time"]
    assert s.by_category(SCOPE, LessonCategory.BEST_KNOWN_PATH) == ["stage tests before deliver"]
    assert s.by_category(SCOPE, LessonCategory.USER_PREFERENCE) == []


def test_report_metadata_category_is_honored(tmp_path):
    s = _store(tmp_path)
    s.report_lesson(SCOPE, "answer in German", {"category": "user_preference"})
    assert s.by_category(SCOPE, LessonCategory.USER_PREFERENCE) == ["answer in German"]


def test_unknown_category_falls_back_to_general(tmp_path):
    s = _store(tmp_path)
    s.report_lesson(SCOPE, "plain", {"category": "nonsense"})
    assert s.by_category(SCOPE, LessonCategory.GENERAL) == ["plain"]


# ─── brief: scope-priority + grouping + dedup + cap ─────────────────────────────────────────────────
def test_brief_groups_by_category_in_order(tmp_path):
    s = _store(tmp_path)
    s.record(SCOPE, "do X first", LessonCategory.BEST_KNOWN_PATH)
    s.record(SCOPE, "never Y", LessonCategory.KNOWN_BAD_STRATEGY)
    out = s.brief([SCOPE])
    # KNOWN_BAD_STRATEGY header precedes BEST_KNOWN_PATH (actionability order), each with its lessons.
    assert out.index("[known_bad_strategy]") < out.index("[best_known_path]")
    assert "- never Y" in out and "- do X first" in out


def test_brief_scope_priority_and_dedup_and_cap(tmp_path):
    s = _store(tmp_path)
    s.report_lesson(SCOPE, "shared")
    s.report_lesson(SCOPE, "only-A")
    s.report_lesson(SCOPE_B, "shared")           # same text in a lower-priority scope → deduped
    s.report_lesson(SCOPE_B, "only-B")
    out = s.brief([SCOPE, SCOPE_B], limit=99)
    assert out.count("shared") == 1              # deduped across scopes
    assert "only-A" in out and "only-B" in out


def test_brief_empty_when_nothing(tmp_path):
    assert _store(tmp_path).brief([SCOPE]) == ""


# ─── forget purge (the acceptance signal) ───────────────────────────────────────────────────────────
def test_forget_purges_scope(tmp_path):
    s = _store(tmp_path)
    s.report_lesson(SCOPE, "to be forgotten")
    s.report_lesson(SCOPE_B, "kept")
    assert s.forget(SCOPE) is True
    assert s.get_lessons(SCOPE) == []            # purged
    assert s.get_lessons(SCOPE_B) == ["kept"]    # the sibling scope is untouched


def test_forget_unknown_scope_is_false(tmp_path):
    assert _store(tmp_path).forget(SCOPE) is False


# ─── persistence + robustness ───────────────────────────────────────────────────────────────────────
def test_persists_across_instances(tmp_path):
    _store(tmp_path).report_lesson(SCOPE, "durable")
    assert _store(tmp_path).get_lessons(SCOPE) == ["durable"]   # a fresh instance reads the same dir


def test_load_invalid_utf8_is_failsoft(tmp_path):
    """A corrupt file with invalid UTF-8 bytes must read as empty (UnicodeDecodeError is a ValueError, not an
    OSError — it must not escape report_lesson/get_lessons)."""
    s = _store(tmp_path)
    s.report_lesson(SCOPE, "ok")
    s._path(SCOPE).write_bytes(b"\xff\xfe\x00 not valid utf-8 \x80\x81")
    assert s.get_lessons(SCOPE) == []          # fail-soft, no UnicodeDecodeError
    s.report_lesson(SCOPE, "recovered")        # and writing recovers cleanly
    assert s.get_lessons(SCOPE) == ["recovered"]


def test_hostile_category_str_never_raises(tmp_path):
    class _BadCat:
        def __str__(self):
            raise RuntimeError("nope")
    s = _store(tmp_path)
    s.report_lesson(SCOPE, "x", {"category": _BadCat()})   # hostile category __str__ → GENERAL, no raise
    assert s.get_lessons(SCOPE) == ["x"]


def test_construct_with_hostile_int_cap_never_raises(tmp_path):
    class _BadInt:
        def __int__(self):
            raise RuntimeError("nope")
    s = EngineLessonStore(tmp_path / "l", max_per_scope=_BadInt())   # must not raise (default cap)
    s.report_lesson(SCOPE, "x")
    assert s.get_lessons(SCOPE) == ["x"]


def test_save_failure_leaves_no_orphan_tmp(tmp_path, monkeypatch):
    """A failed os.replace must not leave an orphaned .tmp file behind."""
    import lesson_store as _ls
    s = _store(tmp_path)
    monkeypatch.setattr(_ls.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    s.report_lesson(SCOPE, "x")                 # write succeeds, replace fails → no raise (fail-soft)
    base = tmp_path / "lessons"
    assert base.exists()
    assert not list(base.glob("*.tmp"))        # the temp was cleaned up


def test_hostile_str_scope_never_raises(tmp_path):
    """A hostile str subclass (raising strip()) as scope must no-op every scope method, never raise."""
    class _BadScope(str):
        def strip(self, *a):
            raise RuntimeError("nope")
    bad = _BadScope("x")
    s = _store(tmp_path)
    s.report_lesson(bad, "x")                      # write
    assert s.get_lessons(bad) == []
    assert s.by_category(bad, LessonCategory.BEST_KNOWN_PATH) == []
    assert s.brief([bad]) == ""
    assert s.forget(bad) is False


def test_hostile_str_lesson_never_raises(tmp_path):
    class _BadLesson(str):
        def strip(self, *a):
            raise RuntimeError("nope")
    s = _store(tmp_path)
    s.report_lesson(SCOPE, _BadLesson("x"))        # hostile lesson.strip() → swallowed, no raise
    assert s.get_lessons(SCOPE) == []              # nothing stored (the hostile lesson was skipped)


def test_record_hostile_inputs_never_raise(tmp_path):
    """record()/by_category() must not raise on a hostile category (__class__ raises) or metadata."""
    class _BadCategory:
        @property
        def __class__(self):
            raise RuntimeError("nope")
    s = _store(tmp_path)
    s.record(SCOPE, "x", _BadCategory())                 # hostile category → swallowed, no raise
    s.by_category(SCOPE, _BadCategory())                 # hostile category on the read path too


def test_construct_with_bad_base_dir_never_raises(tmp_path):
    class _BadPath:
        def __fspath__(self):
            raise RuntimeError("nope")
    s = EngineLessonStore(_BadPath())                    # Path(base_dir) would raise → inert sentinel, no raise
    assert s.get_lessons(SCOPE) == []                    # unusable but inert, never raises
    s.report_lesson(SCOPE, "x")                          # still no raise


def test_corrupt_scope_file_reads_as_empty(tmp_path):
    s = _store(tmp_path)
    s.report_lesson(SCOPE, "ok")
    path = s._path(SCOPE)                          # internal: corrupt the on-disk record
    path.write_text("{ this is not json", encoding="utf-8")
    assert s.get_lessons(SCOPE) == []             # fail-soft, no raise
    s.report_lesson(SCOPE, "recovered")           # and writing recovers cleanly
    assert s.get_lessons(SCOPE) == ["recovered"]


def test_weird_opaque_scope_gets_safe_filename(tmp_path):
    s = _store(tmp_path)
    weird = "a/b:c\\d::e f*?"                      # chars illegal in a filename on some OSes
    s.report_lesson(weird, "survives")
    assert s.get_lessons(weird) == ["survives"]
    assert s._path(weird).suffix == ".json"       # hashed → safe


def test_non_serializable_metadata_never_raises(tmp_path):
    """A direct caller passing non-JSON metadata must NOT make report_lesson raise (fail-soft); the bad
    entry is dropped, the lesson + the good metadata persist."""
    s = _store(tmp_path)
    s.report_lesson(SCOPE, "x", {"bad": object(), "category": "user_preference", "ok": 1})
    assert s.get_lessons(SCOPE) == ["x"]                       # stored, did not raise
    assert s.by_category(SCOPE, LessonCategory.USER_PREFERENCE) == ["x"]   # good 'category' survived
    assert _store(tmp_path).get_lessons(SCOPE) == ["x"]        # and it round-tripped to disk


def test_construct_with_bad_cap_uses_default(tmp_path):
    s = EngineLessonStore(tmp_path / "l", max_per_scope="bad")   # must not raise
    s.report_lesson(SCOPE, "x")
    assert s.get_lessons(SCOPE) == ["x"]


def test_configure_ignores_bad_cap(tmp_path):
    s = _store(tmp_path, max_per_scope=3)
    s.configure(max_per_scope="bad")                            # must not raise; keeps the prior cap (3)
    s.configure(max_per_scope=float("inf"))                     # int(inf) → OverflowError; must not raise
    for i in range(5):
        s.report_lesson(SCOPE, f"l{i}")
    assert len(s.get_lessons(SCOPE, limit=99)) == 3


def test_construct_with_overflow_cap_uses_default(tmp_path):
    s = EngineLessonStore(tmp_path / "l", max_per_scope=float("inf"))   # int(inf) → OverflowError; no raise
    s.report_lesson(SCOPE, "x")
    assert s.get_lessons(SCOPE) == ["x"]


def test_by_category_empty_scope_is_empty(tmp_path):
    # an empty scope has no partition → [] (matches get_lessons; no hash-of-"" file read).
    assert _store(tmp_path).by_category("", LessonCategory.BEST_KNOWN_PATH) == []


def test_blank_scope_reads_no_partition(tmp_path):
    """A blank/whitespace/None scope has NO partition — none of the readers may even resolve a file path for
    it. Proven by RECORDING every _path call and asserting none (a raising spy would be swallowed by the
    methods' broad except, so it must be a recorder, not a thrower)."""
    s = _store(tmp_path)
    seen = []
    real_path = s._path
    s._path = lambda scope: (seen.append(scope), real_path(scope))[1]
    for blank in ("", "   ", None):
        assert s.get_lessons(blank) == []
        assert s.by_category(blank, LessonCategory.BEST_KNOWN_PATH) == []
    assert s.brief(["", "  ", None]) == ""
    assert seen == []        # _path was NEVER resolved for any blank scope (read + brief)


def test_empty_scope_or_lesson_is_noop(tmp_path):
    s = _store(tmp_path)
    s.report_lesson("", "x")
    s.report_lesson(SCOPE, "")
    s.report_lesson(SCOPE, "   ")
    assert s.get_lessons(SCOPE) == []
    assert s.get_lessons("") == []


# ─── retired engine wiring — ACE is the provider authority ─────────────────────────────────────────
@pytest.fixture
def _clean_provider():
    """Pin ack.lessons to no-provider before+after, so the gating tests are hermetic."""
    from ack import lessons as L
    L.set_provider(None)
    yield L
    L.set_provider(None)


@pytest.mark.parametrize(
    ("dotted", "value"),
    [("lessons.enabled", False), ("lessons.enabled", True),
     ("lessons.max_per_scope", 5), ("lessons.max_per_scope", "anything")],
)
def test_retired_lesson_config_warns_is_ignored_and_runtime_set_is_refused(
        monkeypatch, capsys, dotted, value, _clean_provider):
    import gx10
    gx10._apply_config(gx10._code_defaults())
    provider = _clean_provider.get_provider()
    assert provider is not None

    cfg = gx10._code_defaults()
    gx10._cfg_set(cfg, dotted, value)
    gx10._apply_config(cfg)
    assert dotted in capsys.readouterr().out
    assert "lessons" not in cfg
    assert _clean_provider.get_provider() is provider

    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg)
    surfaced = []
    monkeypatch.setattr(gx10, "_ui_print", lambda message, *a, **k: surfaced.append(str(message)))
    gx10._dispatch(None, f"config set {dotted} {value}")
    assert len(surfaced) == 1 and "retired and cannot be set" in surfaced[0]
    assert _clean_provider.get_provider() is provider


def test_apply_default_config_wires_no_provider(tmp_path, monkeypatch, _clean_provider):
    """The retired compatibility seam cannot replace ACE's provider authority."""
    monkeypatch.setenv("GX10_HOME", str(tmp_path))
    import gx10
    gx10._apply_lessons_provider(gx10._code_defaults())
    assert _clean_provider.get_provider() is None


def test_legacy_lesson_false_cannot_disable_always_on_ace(tmp_path, monkeypatch, _clean_provider):
    monkeypatch.setenv("GX10_HOME", str(tmp_path))
    import gx10
    from playbook_store import PlaybookStore

    cfg = gx10._code_defaults()
    cfg["lessons"] = {"enabled": False, "max_per_scope": 1}
    gx10._apply_config(cfg)

    assert isinstance(_clean_provider.get_provider(), PlaybookStore)
    assert "lessons" not in cfg


def test_apply_does_not_clobber_a_foreign_provider(tmp_path, monkeypatch, _clean_provider):
    monkeypatch.setenv("GX10_HOME", str(tmp_path))
    import gx10

    class _Foreign:
        def get_lessons(self, scope, query="", limit=10): return []
        def report_lesson(self, scope, lesson, metadata=None): pass
        def brief(self, scopes, limit=10): return ""

    foreign = _Foreign()
    _clean_provider.set_provider(foreign)
    gx10._apply_lessons_provider(gx10._code_defaults())
    assert _clean_provider.get_provider() is foreign
    gx10._apply_lessons_provider(gx10._code_defaults())
    assert _clean_provider.get_provider() is foreign
