"""PanelRegistry loader (skills/mpr/registry/loader.py) — discover/validate/resolve (Spec 05 §6 / §10).

Deterministic, filesystem-only (tmp_path), no LLM/network: register+resolve roundtrip, discovery walks
the panels dir, a duplicate domain is fail-loud (DuplicatePanelError, wording mirrors registry.py:338),
a broken file is skipped not fatal (fail-soft), unknown domain → None, missing root → [], a .py without
PANEL is ignored, and the lazy singleton builds on demand.
"""
from __future__ import annotations

import pytest

from mpr.registry.loader import (
    DuplicatePanelError,
    PanelRegistry,
    get_registry,
)
from mpr.registry.schema import Panel


def _panel_src(domain: str, mode: str = "decision", evidence: str = "internal",
               template: str = "decision-matrix") -> str:
    return (
        "from mpr.registry.schema import Panel\n"
        "PANEL = Panel(\n"
        f"    domain={domain!r}, mode={mode!r}, evidence_source={evidence!r},\n"
        f"    synthesis_template={template!r},\n"
        "    effort_defaults={'default': 'medium'},\n"
        "    roles=[\n"
        "        {'role': 'A', 'lens_prompt': 'x'},\n"
        "        {'role': 'B', 'lens_prompt': 'y'},\n"
        "        {'role': 'C', 'lens_prompt': 'z'},\n"
        "    ],\n"
        ")\n"
    )


def _write_panel(panels_dir, name: str, src: str):
    panels_dir.mkdir(parents=True, exist_ok=True)
    f = panels_dir / f"{name}.py"
    f.write_text(src, encoding="utf-8")
    return f


def _make_panel(domain: str, evidence: str = "internal") -> Panel:
    return Panel.model_validate({
        "domain": domain, "mode": "decision", "evidence_source": evidence,
        "synthesis_template": "decision-matrix", "effort_defaults": {"default": "medium"},
        "roles": [{"role": "A", "lens_prompt": "x"}, {"role": "B", "lens_prompt": "y"},
                  {"role": "C", "lens_prompt": "z"}],
    })


# ── register / resolve ───────────────────────────────────────────────────────────────────────────
def test_register_and_resolve_panel():
    reg = PanelRegistry()
    p = _make_panel("architecture-decision")
    reg.register(p, source="mem")
    assert reg.resolve("architecture-decision") is p
    assert reg.domains() == ["architecture-decision"]


def test_resolve_unknown_domain_returns_none():
    assert PanelRegistry().resolve("nope") is None


def test_register_same_source_is_idempotent():
    reg = PanelRegistry()
    reg.register(_make_panel("competitive", "external"), source="f.py")
    reg.register(_make_panel("competitive", "external"), source="f.py")  # same source → no raise
    assert reg.domains() == ["competitive"]


def test_register_duplicate_domain_different_source_fail_loud():
    reg = PanelRegistry()
    reg.register(_make_panel("regulatory", "external"), source="a.py")
    with pytest.raises(DuplicatePanelError, match=r"already registered \(by a\.py\)"):
        reg.register(_make_panel("regulatory", "external"), source="b.py")


def test_domains_sorted():
    reg = PanelRegistry()
    reg.register(_make_panel("competitive", "external"), source="1")
    reg.register(_make_panel("architecture-decision"), source="2")
    assert reg.domains() == ["architecture-decision", "competitive"]


# ── discovery ──────────────────────────────────────────────────────────────────────────────────
def test_discover_walks_panels_dir(tmp_path):
    pdir = tmp_path / "panels"
    _write_panel(pdir, "architecture_decision", _panel_src("architecture-decision"))
    _write_panel(pdir, "competitive", _panel_src("competitive", "comparison", "external", "comparison-matrix"))
    reg = PanelRegistry()
    added = reg.discover(tmp_path)  # plugin root → scans root/panels
    assert {p.domain for p in added} == {"architecture-decision", "competitive"}
    assert reg.resolve("competitive").evidence_source == "external"


def test_discover_accepts_panels_dir_directly(tmp_path):
    _write_panel(tmp_path, "regulatory", _panel_src("regulatory", "evidence-research", "external", "evidence-report"))
    reg = PanelRegistry()
    added = reg.discover(tmp_path)  # no 'panels' subdir → treat root as the panels dir
    assert [p.domain for p in added] == ["regulatory"]


def test_discover_skips_underscore_files(tmp_path):
    pdir = tmp_path / "panels"
    _write_panel(pdir, "architecture_decision", _panel_src("architecture-decision"))
    _write_panel(pdir, "_helper", "X = 1\n")  # underscore → ignored
    reg = PanelRegistry()
    added = reg.discover(tmp_path)
    assert [p.domain for p in added] == ["architecture-decision"]


def test_discover_missing_root_returns_empty():
    assert PanelRegistry().discover("/no/such/dir/xyz") == []


def test_py_without_PANEL_is_ignored(tmp_path):
    pdir = tmp_path / "panels"
    _write_panel(pdir, "notapanel", "SOMETHING = 42\n")  # no PANEL attribute
    reg = PanelRegistry()
    assert reg.discover(tmp_path) == []


def test_explicit_panel_none_is_skipped_not_fatal(tmp_path):
    # PANEL=None (author footgun) is distinguishable from a missing attribute → fail-soft skip,
    # alongside a good panel that still loads.
    pdir = tmp_path / "panels"
    _write_panel(pdir, "oops", "PANEL = None\n")
    _write_panel(pdir, "good", _panel_src("architecture-decision"))
    reg = PanelRegistry()
    added = reg.discover(tmp_path)
    assert [p.domain for p in added] == ["architecture-decision"]


def test_panel_wrong_type_is_skipped_not_fatal(tmp_path):
    pdir = tmp_path / "panels"
    _write_panel(pdir, "junk", "PANEL = 42\n")  # neither Panel nor dict
    reg = PanelRegistry()
    assert reg.discover(tmp_path) == []


# ── two error postures ───────────────────────────────────────────────────────────────────────────
def test_broken_panel_file_is_skipped_not_fatal(tmp_path):
    pdir = tmp_path / "panels"
    _write_panel(pdir, "good", _panel_src("architecture-decision"))
    _write_panel(pdir, "broken", "def (((  this is not valid python\n")  # syntax error
    reg = PanelRegistry()
    added = reg.discover(tmp_path)  # broken skipped, good loaded — no raise
    assert [p.domain for p in added] == ["architecture-decision"]


def test_invalid_panel_object_is_skipped_not_fatal(tmp_path):
    pdir = tmp_path / "panels"
    # PANEL is a dict but invalid (1 role < MIN_ROLES) → ValidationError → fail-soft skip
    _write_panel(pdir, "bad", "PANEL = {'domain': 'x', 'mode': 'decision', 'roles': []}\n")
    _write_panel(pdir, "good", _panel_src("competitive", "comparison", "external", "comparison-matrix"))
    reg = PanelRegistry()
    added = reg.discover(tmp_path)
    assert [p.domain for p in added] == ["competitive"]


def test_duplicate_domain_across_files_is_fail_loud(tmp_path):
    pdir = tmp_path / "panels"
    _write_panel(pdir, "a", _panel_src("architecture-decision"))
    _write_panel(pdir, "b", _panel_src("architecture-decision"))  # same domain, different file
    reg = PanelRegistry()
    with pytest.raises(DuplicatePanelError, match="already registered"):
        reg.discover(tmp_path)


# ── lazy singleton (§6.3) ──────────────────────────────────────────────────────────────────────
def test_get_registry_lazy_singleton(tmp_path):
    pdir = tmp_path / "panels"
    _write_panel(pdir, "architecture_decision", _panel_src("architecture-decision"))
    reg1 = get_registry(tmp_path, rediscover=True)
    assert reg1.resolve("architecture-decision") is not None
    reg2 = get_registry()  # no rediscover → same instance reused
    assert reg2 is reg1
