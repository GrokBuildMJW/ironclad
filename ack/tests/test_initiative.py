"""STATE-Layout Unit B1: the Initiative core — initiative-centric vault under vault/<slug>/.

A Initiative is a visible knowledge/work unit; meta.md (flat frontmatter) is the SSOT and the single
active initiative is a slug in state_root()/active. Artifact-producing ops resolve relative to the
active initiative (B3) and are fail-closed without one. These tests cover CRUD + the active marker +
the type-dependent skeleton, all workdir-relative (chdir to a tmp project root per test).
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
    assert gx10._slugify("") == "initiative"        # never empty
    assert gx10._slugify("***") == "initiative"


# ── new(): skeleton + meta + active ───────────────────────────
def test_new_software_creates_skeleton_meta_and_active(tmp_path):
    v = gx10.initiative_new("Order Service", "software")
    assert v.slug == "order-service"
    assert v.type == "software"
    base = tmp_path / "vault" / "order-service"
    assert (base / "meta.md").is_file()
    # Hybrid layout (B3): visible artifacts flat, machine plumbing hidden under .work/
    for d in ("tasks/pending", "tasks/in_progress", "tasks/done",
              "decisions", "proposals", "reviews",
              ".work/handovers", ".work/feedback",
              ".work/archive/handovers", ".work/archive/feedback"):
        assert (base / d).is_dir(), f"missing skeleton dir {d}"
    # active marker points at the new initiative
    assert (tmp_path / ".ironclad" / "active").read_text(encoding="utf-8").strip() == "order-service"
    assert gx10.active_slug() == "order-service"


def test_new_mpr_creates_runs_and_decisions(tmp_path):
    v = gx10.initiative_new("Risk Review", "mpr")
    assert v.type == "mpr"
    base = tmp_path / "vault" / "risk-review"
    assert (base / "runs").is_dir()
    assert (base / "decisions").is_dir()
    # mpr has NO software task-pipeline
    assert not (base / "tasks").exists()


def test_vault_files_written_with_lf(tmp_path):
    """Vault files + the active marker must use deterministic LF endings (newline='\\n') so a
    Windows desktop and a Linux Spark produce byte-identical files (no CRLF/LF drift). Guards #8."""
    gx10.initiative_new("LF Check", "mpr")
    base = tmp_path / "vault" / "lf-check"
    for rel in ("meta.md", "INDEX.md"):
        assert b"\r\n" not in (base / rel).read_bytes(), f"{rel} contains CRLF"
    assert b"\r\n" not in (tmp_path / ".ironclad" / "active").read_bytes()


def test_created_date_uses_local_calendar_day(tmp_path):
    # #10: the visible created date follows the local calendar day (localtime), not UTC
    import time as _t
    v = gx10.initiative_new("Local Date", "mpr")
    assert v.created == _t.strftime("%Y-%m-%d", _t.localtime())


def test_meta_frontmatter_roundtrips(tmp_path):
    gx10.initiative_new("Mein Projekt", "software")
    v = gx10.initiative_get("mein-projekt")
    assert v is not None
    assert v.type == "software"
    assert v.title == "Mein Projekt"
    assert v.status == "active"
    assert v.created and v.created[:2] == "20"   # ISO date written


# ── collision handling ────────────────────────────────────────
def test_new_collision_gets_suffix(tmp_path):
    a = gx10.initiative_new("Same Name", "software")
    b = gx10.initiative_new("Same Name", "mpr")
    assert a.slug == "same-name"
    assert b.slug == "same-name-2"
    assert (tmp_path / "vault" / "same-name").is_dir()
    assert (tmp_path / "vault" / "same-name-2").is_dir()
    # newest becomes active
    assert gx10.active_slug() == "same-name-2"


# ── list ──────────────────────────────────────────────────────
def test_list_returns_all_sorted():
    gx10.initiative_new("Beta", "software")
    gx10.initiative_new("Alpha", "mpr")
    slugs = [v.slug for v in gx10.initiative_list()]
    assert slugs == ["alpha", "beta"]   # sorted by slug


def test_list_empty_when_no_vault():
    assert gx10.initiative_list() == []


# ── use / active ──────────────────────────────────────────────
def test_use_switches_active():
    gx10.initiative_new("First", "software")
    gx10.initiative_new("Second", "mpr")    # now active
    assert gx10.active_slug() == "second"
    v = gx10.initiative_use("first")
    assert v.slug == "first"
    assert gx10.active_slug() == "first"
    assert gx10.initiative_active().slug == "first"


def test_use_unknown_raises():
    with pytest.raises(ValueError):
        gx10.initiative_use("does-not-exist")


def test_active_none_when_unset():
    assert gx10.active_slug() is None
    assert gx10.initiative_active() is None


def test_active_none_when_marker_dangling(tmp_path):
    gx10.set_active_slug("ghost")          # marker points at a non-existent initiative
    assert gx10.active_slug() == "ghost"
    assert gx10.initiative_active() is None   # resolves to None, not a crash


# ── fail-closed routing source (B3 foundation) ────────────────
def test_active_initiative_path_failclosed_without_active():
    with pytest.raises(RuntimeError):
        gx10.active_initiative_path()


def test_active_initiative_path_returns_active(tmp_path):
    gx10.initiative_new("Routed", "software")
    # workdir-relative by design (the engine chdir's to the workdir once at boot, then stays)
    assert gx10.active_initiative_path() == Path("vault") / "routed"
    assert (gx10.active_initiative_path()).resolve() == (tmp_path / "vault" / "routed").resolve()


# ── validation ────────────────────────────────────────────────
def test_new_invalid_typ_raises():
    with pytest.raises(ValueError):
        gx10.initiative_new("X", "database")


def test_new_empty_name_raises():
    with pytest.raises(ValueError):
        gx10.initiative_new("   ", "software")


# ── B2: CLI command surface (_initiative_command) ───────────────
def test_cmd_new_creates_and_reports(tmp_path):
    out = gx10._initiative_command("new Order Service --type software")
    assert "order-service" in out and "software" in out
    assert (tmp_path / "vault" / "order-service" / "meta.md").is_file()
    assert gx10.active_slug() == "order-service"


def test_cmd_new_message_is_type_aware(tmp_path):
    # #13: message names the artefacts actually seeded for the type (no Tasks/Handovers for mpr),
    # and mpr appends the activation hint (mpr.enabled defaults off in tests).
    mpr = gx10._initiative_command("new Risk Panel --type mpr")
    assert "runs" in mpr and "decisions" in mpr
    assert "Tasks" not in mpr and "Handovers" not in mpr
    assert "mpr.enabled" in mpr
    soft = gx10._initiative_command("new Order Svc --type software")
    assert "tasks" in soft and "reviews" in soft
    assert "mpr.enabled" not in soft


def test_cmd_new_typ_position_independent_and_eq():
    out = gx10._initiative_command("new --type=mpr Risk Panel")
    assert "risk-panel" in out and "mpr" in out
    assert gx10.initiative_get("risk-panel").type == "mpr"


def test_cmd_new_without_typ_shows_usage():
    out = gx10._initiative_command("new Just A Name")
    assert "usage" in out.lower() and "--type" in out
    assert gx10.initiative_list() == []   # nothing created


def test_cmd_new_invalid_typ_failclosed():
    out = gx10._initiative_command("new X --type database")
    assert "[initiative]" in out and "database" in out   # clear error, not a crash
    assert gx10.initiative_list() == []


def test_cmd_list_marks_active():
    gx10._initiative_command("new One --type software")
    gx10._initiative_command("new Two --type mpr")     # active
    out = gx10._initiative_command("list")
    assert "* two" in out
    assert "  one" in out and "* one" not in out


def test_cmd_list_empty():
    assert "keine" in gx10._initiative_command("list")
    assert "keine" in gx10._initiative_command("")    # bare → list


def test_cmd_use_and_unknown():
    gx10._initiative_command("new Alpha --type software")
    gx10._initiative_command("new Beta --type mpr")
    assert "alpha" in gx10._initiative_command("use alpha")
    assert gx10.active_slug() == "alpha"
    out = gx10._initiative_command("use nope")
    assert "[initiative]" in out and "nope" in out     # fail-closed message


def test_cmd_active_and_reconcile():
    assert "keins active" in gx10._initiative_command("active")
    gx10._initiative_command("new Solo --type software")
    assert "solo" in gx10._initiative_command("active")
    # reconcile_vault is wired (Unit C) → the command actually reconciles now
    assert "indiziert" in gx10._initiative_command("reconcile")


def test_cmd_unknown_sub_shows_usage():
    assert "usage" in gx10._initiative_command("frobnicate").lower()


# ── B2: dispatch routes /initiative as a command (no model turn) ──
class _FakeAgent:
    def __init__(self):
        self.ran = None
        self.saved = 0

    def run(self, text):
        self.ran = text

    def save_session(self):
        self.saved += 1


def test_dispatch_initiative_is_a_command_not_a_turn():
    a = _FakeAgent()
    gx10._dispatch(a, "initiative list")
    assert a.ran is None and a.saved == 0      # handled as a command, no model call


def test_dispatch_initiative_new_routes_to_command(tmp_path):
    a = _FakeAgent()
    gx10._dispatch(a, "initiative new Routed --type software")
    assert a.ran is None and a.saved == 0
    assert (tmp_path / "vault" / "routed" / "meta.md").is_file()


# ── #15: type is a real contract — mpr refuses the task pipeline ──
def test_mpr_initiative_refuses_task_pipeline(tmp_path):
    gx10.initiative_new("Risk Panel", "mpr")
    s = gx10._stage_handover(None, "OPUS", "## Handover\nbody",
                             task_json='{"type":"feature","priority":"high","title":"x","description":"y"}')
    assert s.startswith("ERROR") and "mpr" in s.lower()
    a = gx10._advance_pipeline(f"{gx10.TASK_PREFIX}-1", "OPUS")
    assert a.startswith("ERROR") and "mpr" in a.lower()
    with pytest.raises(RuntimeError):
        gx10._store().create({"type": "feature", "priority": "high", "title": "x", "description": "y"}, force=True)
    # mpr seeds no task pipeline dirs
    assert not (tmp_path / "vault" / "risk-panel" / "tasks").exists()
    assert not (tmp_path / "vault" / "risk-panel" / ".work").exists()


def test_software_initiative_allows_task_pipeline(tmp_path):
    gx10.initiative_new("Order Svc", "software")
    tid = gx10._store().create(
        {"type": "feature", "priority": "high", "title": "x", "description": "y"}, force=True)["id"]
    out = gx10._stage_handover(tid, "OPUS", "## Handover\nbody")
    assert not out.startswith("ERROR")
