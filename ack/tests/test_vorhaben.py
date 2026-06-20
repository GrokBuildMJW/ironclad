"""STATE-Layout Unit B1: the Vorhaben core — vorhaben-centric vault under vault/<slug>/.

A Vorhaben is a visible knowledge/work unit; meta.md (flat frontmatter) is the SSOT and the single
active vorhaben is a slug in state_root()/active. Artifact-producing ops resolve relative to the
active vorhaben (B3) and are fail-closed without one. These tests cover CRUD + the active marker +
the typ-dependent skeleton, all workdir-relative (chdir to a tmp project root per test).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402


@pytest.fixture(autouse=True)
def _in_project(tmp_path, monkeypatch):
    """Every test runs in a fresh project root; vault/ and .ironclad/ are workdir-relative."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ── slug formation ────────────────────────────────────────────
def test_slugify_kebab_and_umlaut_fold():
    assert gx10._slugify("Mein Großes Projekt") == "mein-grosses-projekt"
    assert gx10._slugify("Über Größe") == "ueber-groesse"     # German umlauts folded
    assert gx10._slugify("  RAG  Pipeline!! ") == "rag-pipeline"
    assert gx10._slugify("a/b\\c:d") == "a-b-c-d"             # path/punct runs → single "-"
    assert gx10._slugify("a__b  c") == "a-b-c"
    assert gx10._slugify("") == "vorhaben"        # never empty
    assert gx10._slugify("***") == "vorhaben"


# ── new(): skeleton + meta + active ───────────────────────────
def test_new_software_creates_skeleton_meta_and_active(tmp_path):
    v = gx10.vorhaben_new("Order Service", "software")
    assert v.slug == "order-service"
    assert v.typ == "software"
    base = tmp_path / "vault" / "order-service"
    assert (base / "meta.md").is_file()
    # Hybrid layout (B3): visible artifacts flat, machine plumbing hidden under .work/
    for d in ("tasks/pending", "tasks/in_progress", "tasks/done",
              "decisions", "proposals", "reviews",
              ".work/handovers", ".work/feedback",
              ".work/archive/handovers", ".work/archive/feedback"):
        assert (base / d).is_dir(), f"missing skeleton dir {d}"
    # active marker points at the new vorhaben
    assert (tmp_path / ".ironclad" / "active").read_text(encoding="utf-8").strip() == "order-service"
    assert gx10.active_slug() == "order-service"


def test_new_mpr_creates_runs_and_decisions(tmp_path):
    v = gx10.vorhaben_new("Risk Review", "mpr")
    assert v.typ == "mpr"
    base = tmp_path / "vault" / "risk-review"
    assert (base / "runs").is_dir()
    assert (base / "decisions").is_dir()
    # mpr has NO software task-pipeline
    assert not (base / "tasks").exists()


def test_meta_frontmatter_roundtrips(tmp_path):
    gx10.vorhaben_new("Mein Projekt", "software")
    v = gx10.vorhaben_get("mein-projekt")
    assert v is not None
    assert v.typ == "software"
    assert v.titel == "Mein Projekt"
    assert v.status == "aktiv"
    assert v.erstellt and v.erstellt[:2] == "20"   # ISO date written


# ── collision handling ────────────────────────────────────────
def test_new_collision_gets_suffix(tmp_path):
    a = gx10.vorhaben_new("Same Name", "software")
    b = gx10.vorhaben_new("Same Name", "mpr")
    assert a.slug == "same-name"
    assert b.slug == "same-name-2"
    assert (tmp_path / "vault" / "same-name").is_dir()
    assert (tmp_path / "vault" / "same-name-2").is_dir()
    # newest becomes active
    assert gx10.active_slug() == "same-name-2"


# ── list ──────────────────────────────────────────────────────
def test_list_returns_all_sorted():
    gx10.vorhaben_new("Beta", "software")
    gx10.vorhaben_new("Alpha", "mpr")
    slugs = [v.slug for v in gx10.vorhaben_list()]
    assert slugs == ["alpha", "beta"]   # sorted by slug


def test_list_empty_when_no_vault():
    assert gx10.vorhaben_list() == []


# ── use / active ──────────────────────────────────────────────
def test_use_switches_active():
    gx10.vorhaben_new("First", "software")
    gx10.vorhaben_new("Second", "mpr")    # now active
    assert gx10.active_slug() == "second"
    v = gx10.vorhaben_use("first")
    assert v.slug == "first"
    assert gx10.active_slug() == "first"
    assert gx10.vorhaben_active().slug == "first"


def test_use_unknown_raises():
    with pytest.raises(ValueError):
        gx10.vorhaben_use("does-not-exist")


def test_active_none_when_unset():
    assert gx10.active_slug() is None
    assert gx10.vorhaben_active() is None


def test_active_none_when_marker_dangling(tmp_path):
    gx10.set_active_slug("ghost")          # marker points at a non-existent vorhaben
    assert gx10.active_slug() == "ghost"
    assert gx10.vorhaben_active() is None   # resolves to None, not a crash


# ── fail-closed routing source (B3 foundation) ────────────────
def test_active_vorhaben_path_failclosed_without_active():
    with pytest.raises(RuntimeError):
        gx10.active_vorhaben_path()


def test_active_vorhaben_path_returns_active(tmp_path):
    gx10.vorhaben_new("Routed", "software")
    # workdir-relative by design (the engine chdir's to the workdir once at boot, then stays)
    assert gx10.active_vorhaben_path() == Path("vault") / "routed"
    assert (gx10.active_vorhaben_path()).resolve() == (tmp_path / "vault" / "routed").resolve()


# ── validation ────────────────────────────────────────────────
def test_new_invalid_typ_raises():
    with pytest.raises(ValueError):
        gx10.vorhaben_new("X", "database")


def test_new_empty_name_raises():
    with pytest.raises(ValueError):
        gx10.vorhaben_new("   ", "software")


# ── B2: CLI command surface (_vorhaben_command) ───────────────
def test_cmd_new_creates_and_reports(tmp_path):
    out = gx10._vorhaben_command("new Order Service --typ software")
    assert "order-service" in out and "software" in out
    assert (tmp_path / "vault" / "order-service" / "meta.md").is_file()
    assert gx10.active_slug() == "order-service"


def test_cmd_new_typ_position_independent_and_eq():
    out = gx10._vorhaben_command("new --typ=mpr Risk Panel")
    assert "risk-panel" in out and "mpr" in out
    assert gx10.vorhaben_get("risk-panel").typ == "mpr"


def test_cmd_new_without_typ_shows_usage():
    out = gx10._vorhaben_command("new Just A Name")
    assert "usage" in out.lower() and "--typ" in out
    assert gx10.vorhaben_list() == []   # nothing created


def test_cmd_new_invalid_typ_failclosed():
    out = gx10._vorhaben_command("new X --typ database")
    assert "[vorhaben]" in out and "database" in out   # clear error, not a crash
    assert gx10.vorhaben_list() == []


def test_cmd_list_marks_active():
    gx10._vorhaben_command("new One --typ software")
    gx10._vorhaben_command("new Two --typ mpr")     # active
    out = gx10._vorhaben_command("list")
    assert "* two" in out
    assert "  one" in out and "* one" not in out


def test_cmd_list_empty():
    assert "keine" in gx10._vorhaben_command("list")
    assert "keine" in gx10._vorhaben_command("")    # bare → list


def test_cmd_use_and_unknown():
    gx10._vorhaben_command("new Alpha --typ software")
    gx10._vorhaben_command("new Beta --typ mpr")
    assert "alpha" in gx10._vorhaben_command("use alpha")
    assert gx10.active_slug() == "alpha"
    out = gx10._vorhaben_command("use nope")
    assert "[vorhaben]" in out and "nope" in out     # fail-closed message


def test_cmd_active_and_reconcile():
    assert "keins aktiv" in gx10._vorhaben_command("active")
    gx10._vorhaben_command("new Solo --typ software")
    assert "solo" in gx10._vorhaben_command("active")
    # reconcile_vault is wired (Unit C) → the command actually reconciles now
    assert "indiziert" in gx10._vorhaben_command("reconcile")


def test_cmd_unknown_sub_shows_usage():
    assert "usage" in gx10._vorhaben_command("frobnicate").lower()


# ── B2: dispatch routes /vorhaben as a command (no model turn) ──
class _FakeAgent:
    def __init__(self):
        self.ran = None
        self.saved = 0

    def run(self, text):
        self.ran = text

    def save_session(self):
        self.saved += 1


def test_dispatch_vorhaben_is_a_command_not_a_turn():
    a = _FakeAgent()
    gx10._dispatch(a, "vorhaben list")
    assert a.ran is None and a.saved == 0      # handled as a command, no model call


def test_dispatch_vorhaben_new_routes_to_command(tmp_path):
    a = _FakeAgent()
    gx10._dispatch(a, "vorhaben new Routed --typ software")
    assert a.ran is None and a.saved == 0
    assert (tmp_path / "vault" / "routed" / "meta.md").is_file()
