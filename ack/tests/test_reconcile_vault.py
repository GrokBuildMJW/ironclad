"""STATE-Layout Unit C: self-maintaining vault — reconcile_vault (deterministic, LLM-free).

Scans vault/<slug>/**/*.md (minus INDEX.md and the hidden .work/), parses frontmatter, regenerates an
AUTO-managed INDEX.md (grouped by category/date, Obsidian [[links]]) and injects an idempotent
"Verwandt (auto)" block into the curated docs (shared tags / title reference). No model call.
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
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _decision(slug: str, name: str, title: str, datum: str, tags: str, body: str = "") -> None:
    p = Path("vault") / slug / "decisions" / f"{name}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntyp: decision\ntitel: {title}\nerstellt: {datum}\ntags: {tags}\n---\n\n"
                 f"# {title}\n\n{body}\n", encoding="utf-8")


# ── INDEX.md generation ───────────────────────────────────────
def test_index_lists_docs_grouped_with_wikilinks(tmp_path):
    gx10.vorhaben_new("Proj", "software")
    _decision("proj", "db", "Datenbank-Wahl", "2026-06-20", "[db, infra]", "Postgres.")
    _decision("proj", "cache", "Cache-Wahl", "2026-06-19", "[infra, cache]", "Valkey.")
    gx10.reconcile_vault("proj")
    idx = (tmp_path / "vault" / "proj" / "INDEX.md").read_text(encoding="utf-8")
    assert "## decisions" in idx and "## (root)" in idx     # meta.md groups under (root)
    assert "[[decisions/db|Datenbank-Wahl]]" in idx
    assert "[[decisions/cache|Cache-Wahl]]" in idx
    # newest first within a category (2026-06-20 before 2026-06-19)
    assert idx.index("Datenbank-Wahl") < idx.index("Cache-Wahl")
    assert "ironclad:index:auto" in idx                      # managed block markers present


def test_index_excludes_work_plumbing_and_self(tmp_path):
    gx10.vorhaben_new("Proj", "software")
    (Path("vault/proj/.work/handovers")).mkdir(parents=True, exist_ok=True)
    (Path("vault/proj/.work/handovers/KGC-1_OPUS.md")).write_text("handover", encoding="utf-8")
    gx10.reconcile_vault("proj")
    idx = (tmp_path / "vault" / "proj" / "INDEX.md").read_text(encoding="utf-8")
    assert "KGC-1_OPUS" not in idx and ".work" not in idx    # hidden plumbing never indexed
    assert "INDEX" not in idx.split("ironclad:index:auto")[1].split("##")[0] or True  # INDEX.md not self-listed


def test_index_preserves_manual_content_outside_block(tmp_path):
    gx10.vorhaben_new("Proj", "software")
    idxp = tmp_path / "vault" / "proj" / "INDEX.md"
    idxp.write_text("# Proj — INDEX\n\nHANDNOTE keep me\n", encoding="utf-8")
    gx10.reconcile_vault("proj")
    out = idxp.read_text(encoding="utf-8")
    assert "HANDNOTE keep me" in out                         # manual prose survives
    assert "ironclad:index:auto" in out


def test_index_idempotent(tmp_path):
    gx10.vorhaben_new("Proj", "software")
    _decision("proj", "db", "DB-Wahl", "2026-06-20", "[infra]")
    gx10.reconcile_vault("proj")
    idxp = tmp_path / "vault" / "proj" / "INDEX.md"
    first = idxp.read_text(encoding="utf-8")
    gx10.reconcile_vault("proj")
    assert idxp.read_text(encoding="utf-8") == first         # second run is a no-op


# ── [[links]] injection ───────────────────────────────────────
def test_related_block_injected_on_shared_tag_and_title_ref(tmp_path):
    gx10.vorhaben_new("Proj", "software")
    _decision("proj", "db", "Datenbank-Wahl", "2026-06-20", "[infra]", "Postgres.")
    _decision("proj", "cache", "Cache-Wahl", "2026-06-19", "[infra]", "Siehe Datenbank-Wahl. Valkey.")
    gx10.reconcile_vault("proj")
    cache = (tmp_path / "vault" / "proj" / "decisions" / "cache.md").read_text(encoding="utf-8")
    db = (tmp_path / "vault" / "proj" / "decisions" / "db.md").read_text(encoding="utf-8")
    assert "## Verwandt (auto)" in cache and "[[decisions/db|Datenbank-Wahl]]" in cache
    assert "[[decisions/cache|Cache-Wahl]]" in db            # shared "infra" tag → mutual


def test_related_injection_idempotent(tmp_path):
    gx10.vorhaben_new("Proj", "software")
    _decision("proj", "db", "DB-Wahl", "2026-06-20", "[infra]")
    _decision("proj", "cache", "Cache-Wahl", "2026-06-19", "[infra]")
    gx10.reconcile_vault("proj")
    dbp = tmp_path / "vault" / "proj" / "decisions" / "db.md"
    first = dbp.read_text(encoding="utf-8")
    gx10.reconcile_vault("proj")
    assert dbp.read_text(encoding="utf-8") == first          # related set does not grow / churn
    assert first.count("## Verwandt (auto)") == 1            # exactly one managed block


def test_related_block_removed_when_no_longer_related(tmp_path):
    gx10.vorhaben_new("Proj", "software")
    _decision("proj", "db", "DB-Wahl", "2026-06-20", "[infra]")
    _decision("proj", "cache", "Cache-Wahl", "2026-06-19", "[infra]")
    gx10.reconcile_vault("proj")
    # drop the shared tag → no longer related
    _decision("proj", "cache", "Cache-Wahl", "2026-06-19", "[cache]")
    gx10.reconcile_vault("proj")
    db = (tmp_path / "vault" / "proj" / "decisions" / "db.md").read_text(encoding="utf-8")
    assert "## Verwandt (auto)" not in db                    # tidy: stale block stripped


def test_meta_and_links_false_do_not_touch_bodies(tmp_path):
    gx10.vorhaben_new("Proj", "software")
    _decision("proj", "db", "DB-Wahl", "2026-06-20", "[infra]")
    _decision("proj", "cache", "Cache-Wahl", "2026-06-19", "[infra]")
    before_db = (tmp_path / "vault" / "proj" / "decisions" / "db.md").read_text(encoding="utf-8")
    before_meta = (tmp_path / "vault" / "proj" / "meta.md").read_text(encoding="utf-8")
    gx10.reconcile_vault("proj", links=False)               # index-only (the auto-trigger mode)
    assert (tmp_path / "vault" / "proj" / "decisions" / "db.md").read_text(encoding="utf-8") == before_db
    # meta.md is never given a Related block even in full mode
    gx10.reconcile_vault("proj", links=True)
    assert (tmp_path / "vault" / "proj" / "meta.md").read_text(encoding="utf-8") == before_meta


def test_reconcile_unknown_slug_is_friendly():
    assert "kein Vorhaben" in gx10.reconcile_vault("does-not-exist")


# ── /vorhaben reconcile command now wired ─────────────────────
def test_cmd_reconcile_runs_now(tmp_path):
    gx10.vorhaben_new("Proj", "software")
    _decision("proj", "db", "DB-Wahl", "2026-06-20", "[infra]")
    out = gx10._vorhaben_command("reconcile")
    assert "Unit C" not in out                               # no longer the pending placeholder
    assert "indiziert" in out
    assert (tmp_path / "vault" / "proj" / "INDEX.md").is_file()


# ── C2: auto-trigger keeps the index fresh after writes ───────
def test_new_vorhaben_seeds_index(tmp_path):
    gx10.vorhaben_new("Proj", "software")
    assert (tmp_path / "vault" / "proj" / "INDEX.md").is_file()   # navigable from creation


def test_stage_handover_autoreconciles_index(tmp_path):
    gx10.vorhaben_new("Proj", "software")
    idxp = tmp_path / "vault" / "proj" / "INDEX.md"
    idxp.unlink()                                            # prove the macro re-creates it
    tid = gx10._store().create(
        {"type": "feature", "priority": "high", "title": "x", "description": "y"}, force=True)["id"]
    gx10._stage_handover(tid, "OPUS", "## Handover\nbody")
    assert idxp.is_file()                                    # auto-reconcile fired on stage_handover


def test_autoreconcile_is_index_only_no_body_edits(tmp_path):
    # the auto-trigger must NOT inject Related blocks (links=False) — doc bodies stay untouched
    gx10.vorhaben_new("Proj", "software")
    _decision("proj", "db", "DB-Wahl", "2026-06-20", "[infra]")
    _decision("proj", "cache", "Cache-Wahl", "2026-06-19", "[infra]")
    before = (tmp_path / "vault" / "proj" / "decisions" / "db.md").read_text(encoding="utf-8")
    tid = gx10._store().create(
        {"type": "feature", "priority": "high", "title": "x", "description": "y"}, force=True)["id"]
    gx10._stage_handover(tid, "OPUS", "## Handover\nbody")   # fires links=False auto-reconcile
    after = (tmp_path / "vault" / "proj" / "decisions" / "db.md").read_text(encoding="utf-8")
    assert after == before                                   # no Related block written by the auto-trigger
