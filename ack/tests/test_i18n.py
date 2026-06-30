"""Shared core i18n (`ack.i18n.Localizer`, #107) — file-overlay loader, parameterized locales
dir, English fallback. Works standalone (no MPR / no plugins): this test imports only ack.i18n.
"""
from __future__ import annotations

import json

from ack.i18n import Localizer


def _loc(tmp_path, overlay=None, **kw):
    if overlay is not None:
        (tmp_path / "de.json").write_text(json.dumps(overlay), encoding="utf-8")
    return Localizer(tmp_path, **kw)


def test_english_is_default_no_overlay(tmp_path):
    loc = _loc(tmp_path)
    assert loc.localized("source", "en", "x", "y") == "source"
    assert loc.role_lens("d", "r", "EN lens", "en") == "EN lens"
    assert loc.label("question", None) == "question"  # no en_labels → key itself


def test_overlay_translates_and_falls_back(tmp_path):
    loc = _loc(tmp_path, {
        "roles": {"arch": {"skeptic": "DE Skeptiker"}},
        "labels": {"question": "FRAGE"},
        "messages": {"decline": "DE bitte direkt."},
    }, en_labels={"question": "QUESTION"})
    assert loc.role_lens("arch", "skeptic", "EN", "de") == "DE Skeptiker"
    assert loc.role_lens("arch", "missing", "EN default", "de") == "EN default"   # key fallback
    assert loc.label("question", "de") == "FRAGE"
    assert loc.label("question", "en") == "QUESTION"                               # en_labels
    assert loc.localized("EN msg", "de", "messages", "decline") == "DE bitte direkt."
    assert loc.localized("EN msg", "de", "messages", "nope") == "EN msg"          # path fallback


def test_missing_locale_file_falls_back(tmp_path):
    loc = Localizer(tmp_path)  # no fr.json
    assert loc.localized("EN", "fr", "messages", "x") == "EN"


def test_use_language_drives_t(tmp_path):
    loc = _loc(tmp_path, {"messages": {"failed": "Fehlgeschlagen"}})
    assert loc.t("Failed", "messages", "failed") == "Failed"   # default lang en
    loc.use_language("de")
    assert loc.t("Failed", "messages", "failed") == "Fehlgeschlagen"
    loc.use_language(None)
    assert loc.t("Failed", "messages", "failed") == "Failed"   # None → en


def test_two_localizers_are_independent(tmp_path):
    a = (tmp_path / "a"); b = (tmp_path / "b")
    a.mkdir(); b.mkdir()
    (a / "de.json").write_text(json.dumps({"labels": {"k": "A-DE"}}), encoding="utf-8")
    (b / "de.json").write_text(json.dumps({"labels": {"k": "B-DE"}}), encoding="utf-8")
    la, lb = Localizer(a), Localizer(b)
    assert la.label("k", "de") == "A-DE" and lb.label("k", "de") == "B-DE"


def test_broken_overlay_is_harmless(tmp_path):
    (tmp_path / "de.json").write_text("{ not json", encoding="utf-8")
    loc = Localizer(tmp_path)
    assert loc.localized("EN", "de", "x") == "EN"


def test_malformed_nested_overlay_falls_back_not_raises(tmp_path):
    # I18N-1 (#503): a malformed overlay whose nested level is NOT a dict must not AttributeError on the
    # chained .get().get() in role_lens/label — fall back to the English default (never-break-a-run).
    loc = _loc(tmp_path, {"roles": "garbage", "labels": "garbage"}, en_labels={"q": "QUESTION"})
    assert loc.role_lens("arch", "skeptic", "EN", "de") == "EN"   # not a crash
    assert loc.label("q", "de") == "QUESTION"                      # English fallback, not a crash
