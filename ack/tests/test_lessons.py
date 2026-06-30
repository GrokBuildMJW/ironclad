from __future__ import annotations

import pytest
from ack import lessons as L


class StubProvider:
    def __init__(self):
        self.store = {}

    def get_lessons(self, scope, query="", limit=10):
        # store holds (lesson, metadata) tuples; return just the lesson strings
        return [lesson for (lesson, _m) in list(self.store.get(scope, []))][:limit]

    def report_lesson(self, scope, lesson, metadata=None):
        self.store.setdefault(scope, []).append((lesson, metadata))

    def brief(self, scopes, limit=10):
        return "BRIEF:" + ",".join(s for s in scopes)


class NoBriefProvider:  # no brief() → public brief() falls back to a composed digest
    def __init__(self, store):
        self.store = store

    def get_lessons(self, scope, query="", limit=10):
        return list(self.store.get(scope, []))[:limit]

    def report_lesson(self, scope, lesson, metadata=None):
        self.store.setdefault(scope, []).append(lesson)


class RaisingProvider:
    def get_lessons(self, scope, query="", limit=10):
        raise RuntimeError("boom")

    def report_lesson(self, scope, lesson, metadata=None):
        raise RuntimeError("boom")

    def brief(self, scopes, limit=10):
        raise RuntimeError("boom")


class GarbageProvider:  # returns non-list / non-str garbage; the API must never leak it or crash
    def __init__(self, value):
        self._value = value

    def get_lessons(self, scope, query="", limit=10):
        return self._value

    def report_lesson(self, scope, lesson, metadata=None):
        pass


@pytest.fixture(autouse=True)
def _reset_provider():
    L.set_provider(None)
    yield
    L.set_provider(None)


def test_no_provider_is_noop():
    assert L.get_provider() is None
    assert L.get_lessons("s") == []
    assert L.report_lesson("s", "x") is None
    assert L.brief(["s"]) == ""


def test_set_get_provider_roundtrip():
    st = StubProvider()
    L.set_provider(st)
    assert L.get_provider() is st
    L.set_provider(None)
    assert L.get_provider() is None


def test_stub_is_lessonprovider_runtime_checkable():
    assert isinstance(StubProvider(), L.LessonProvider)


def test_get_report_delegated():
    st = StubProvider()
    L.set_provider(st)
    L.report_lesson("p", "use X", {"k": 1})
    assert L.get_lessons("p") == ["use X"]
    assert st.store["p"][0] == ("use X", {"k": 1})


def test_get_limit_passed():
    st = StubProvider()
    st.store["p"] = [("a", None), ("b", None), ("c", None)]   # tuples, matching report_lesson's shape
    L.set_provider(st)
    assert L.get_lessons("p", limit=2) == ["a", "b"]


def test_failsoft_get_swallows_provider_error():
    L.set_provider(RaisingProvider())
    assert L.get_lessons("p") == []


def test_failsoft_report_swallows_provider_error():
    L.set_provider(RaisingProvider())
    assert L.report_lesson("p", "x") is None  # no raise


def test_brief_delegates_when_provider_has_it():
    L.set_provider(StubProvider())
    assert L.brief(["a", "b"]) == "BRIEF:a,b"


def test_brief_composed_fallback_priority_and_dedup():
    L.set_provider(NoBriefProvider({"a": ["la1", "la2"], "b": ["lb1", "la1"]}))
    out = L.brief(["a", "b"], limit=10)
    lines = out.splitlines()
    assert lines == ["la1", "la2", "lb1"]  # a first, la1 deduped


def test_brief_composed_respects_limit():
    L.set_provider(NoBriefProvider({"a": ["1", "2", "3"], "b": ["4", "5"]}))
    assert len(L.brief(["a", "b"], limit=2).splitlines()) == 2


def test_promote_requires_redactor():
    st = StubProvider()
    L.set_provider(st)
    with pytest.raises(ValueError):
        L.promote("x", "a", "b", redactor=None)


def test_promote_refused_when_redactor_returns_none_or_empty_or_nonstr():
    st = StubProvider()
    L.set_provider(st)
    for bad in (
        lambda l, f, t: None,
        lambda l, f, t: "",
        lambda l, f, t: "  ",
        lambda l, f, t: 123,
    ):
        with pytest.raises(ValueError):
            L.promote("secret", "a", "b", redactor=bad)
    assert st.store == {}  # nothing promoted


def test_promote_approved_reports_redacted_tagged():
    st = StubProvider()
    L.set_provider(st)
    out = L.promote(
        "secret /home/x",
        "projA",
        "curated",
        redactor=lambda l, f, t: "[redacted]",
    )
    assert out == "[redacted]"
    assert st.store["curated"][0] == ("[redacted]", {"promoted_from": "projA"})


def test_promote_gate_is_failclosed_without_provider():
    assert L.get_provider() is None
    with pytest.raises(ValueError):
        L.promote("x", "a", "b", redactor=lambda l, f, t: None)  # gate still refuses with no provider


def test_promote_omitted_redactor_raises_valueerror():
    # omitting the redactor entirely must raise ValueError (not TypeError) — fail-closed
    L.set_provider(StubProvider())
    with pytest.raises(ValueError):
        L.promote("x", "a", "b")


def test_get_lessons_rejects_scalar_str_garbage():
    # a provider returning a scalar str must NOT iterate into chars; reject → []
    L.set_provider(GarbageProvider("abc"))
    assert L.get_lessons("p") == []


def test_get_lessons_filters_non_str_items():
    L.set_provider(GarbageProvider(["ok", 123, ["nested"], None, "fine"]))
    assert L.get_lessons("p") == ["ok", "fine"]


def test_get_lessons_rejects_non_list_garbage():
    for junk in ({"k": "v"}, 42, object(), b"bytes"):
        L.set_provider(GarbageProvider(junk))
        assert L.get_lessons("p") == []


def test_brief_failsoft_on_garbage_provider():
    # unhashable / non-str items must not crash the composed digest
    L.set_provider(GarbageProvider(["a", ["unhashable"], "b"]))
    assert L.brief(["p"]) == "a\nb"


def test_api_surface():
    assert L.__version__
    for name in (
        "LessonProvider",
        "set_provider",
        "get_provider",
        "get_lessons",
        "report_lesson",
        "brief",
        "promote",
        "__version__",
    ):
        assert name in L.__all__
