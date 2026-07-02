from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import project_context as pc
from project_context import ProjectContext
import messages
import gx10


@pytest.fixture
def lang_de(monkeypatch):
    monkeypatch.setattr(gx10, "LANGUAGE", "de", raising=False)


def test_messages_keys_present_en_and_de():
    for key in (
        "vault.index_auto",
        "vault.lifecycle_auto",
        "vault.related_heading",
        "vault.related_suffix",
        "vault.index_only_suffix",
        "vault.indexed",
        "vault.no_initiative",
        "init.unknown_type",
        "init.needs_name",
        "init.unknown_slug",
        "init.meta_body",
    ):
        en = messages.msg(key, lang="en")
        de = messages.msg(key, lang="de")
        assert en and not en.startswith("[")
        assert de and not de.startswith("[")


def test_reconcile_english_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "LANGUAGE", "en", raising=False)
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v = gx10.initiative_new("Demo", "software")
        out = gx10.reconcile_vault(v.slug, links=False)
        assert "indexed" in out and "index only" in out
        idx = (v.path / "INDEX.md").read_text(encoding="utf-8")
        assert "Auto-maintained" in idx


def test_reconcile_german_when_language_de(lang_de, tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v = gx10.initiative_new("Demo", "software")
        out = gx10.reconcile_vault(v.slug, links=False)
        assert "indiziert" in out
        idx = (v.path / "INDEX.md").read_text(encoding="utf-8")
        assert "Automatisch gepflegt" in idx


def test_html_markers_frozen_regardless_of_language(lang_de, tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v = gx10.initiative_new("Demo", "software")
        gx10.reconcile_vault(v.slug, links=True)
        idx = (v.path / "INDEX.md").read_text(encoding="utf-8")
        assert "ironclad:index:auto" in idx                       # machine marker never localized
        life = (v.path / "LIFECYCLE.md").read_text(encoding="utf-8")
        assert "ironclad:lifecycle:auto" in life                  # frozen under language=de too


def test_initiative_error_english_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "LANGUAGE", "en", raising=False)
    monkeypatch.chdir(tmp_path)
    import pytest as _pt

    with _pt.raises(ValueError) as ei:
        gx10.initiative_new("", "software")
    assert "needs a name" in str(ei.value)


def test_initiative_error_german_when_de(lang_de, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    import pytest as _pt

    with _pt.raises(ValueError) as ei:
        gx10.initiative_new("", "software")
    assert "braucht einen Namen" in str(ei.value)


def test_no_initiative_message_localized(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "LANGUAGE", "en", raising=False)
    monkeypatch.chdir(tmp_path)
    assert "no initiative" in gx10.reconcile_vault("nope")
