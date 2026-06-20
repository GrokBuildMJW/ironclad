"""Versioning & extension contract (Spec 05 §9).

Proves the "discovery, not hardcoding" extension model: a NEW panel file is picked up by discover with
no loader/router/schema edit; Panel.version defaults to 1, is bumpable, and survives discovery; the
start-panels carry a valid version (migration guard); a duplicate domain stays fail-loud; and
PanelRegistry.versions() reports domain→version (the value the audit manifest in 1d records).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mpr.registry.loader import DuplicatePanelError, PanelRegistry
from mpr.registry.schema import Panel

_MPR_ROOT = Path(__file__).resolve().parents[1]
_EXPECTED = {"architecture-decision", "regulatory", "competitive", "risk-assessment"}


def _panel_src(domain: str, version: int = 1) -> str:
    return (
        "from mpr.registry.schema import Panel\n"
        "PANEL = Panel(\n"
        f"    domain={domain!r}, mode='decision', evidence_source='internal',\n"
        "    synthesis_template='decision-matrix', effort_defaults={'default': 'medium'},\n"
        f"    version={version},\n"
        "    roles=[\n"
        "        {'role': 'A', 'lens_prompt': 'erste Brille'},\n"
        "        {'role': 'B', 'lens_prompt': 'zweite Brille'},\n"
        "        {'role': 'C', 'lens_prompt': 'dritte Brille'},\n"
        "    ],\n"
        ")\n"
    )


def _write(panels_dir: Path, name: str, src: str) -> Path:
    panels_dir.mkdir(parents=True, exist_ok=True)
    f = panels_dir / f"{name}.py"
    f.write_text(src, encoding="utf-8")
    return f


def _registry() -> PanelRegistry:
    reg = PanelRegistry()
    reg.discover(_MPR_ROOT)
    return reg


# ── extension = new file, no code edit ───────────────────────────────────────────────────────────
def test_new_domain_is_discovered_without_code_edit(tmp_path):
    pdir = tmp_path / "panels"
    _write(pdir, "novel_domain", _panel_src("novel-domain"))
    reg = PanelRegistry()
    added = reg.discover(tmp_path)
    assert [p.domain for p in added] == ["novel-domain"]  # picked up purely by discovery
    assert reg.resolve("novel-domain") is not None


# ── Panel.version semantics ──────────────────────────────────────────────────────────────────────
def test_panel_version_defaults_to_one():
    p = Panel.model_validate({
        "domain": "x", "mode": "decision", "evidence_source": "internal",
        "synthesis_template": "decision-matrix", "effort_defaults": {"default": "low"},
        "roles": [{"role": "A", "lens_prompt": "a"}, {"role": "B", "lens_prompt": "b"},
                  {"role": "C", "lens_prompt": "c"}],
    })
    assert p.version == 1


def test_panel_version_survives_discovery(tmp_path):
    pdir = tmp_path / "panels"
    _write(pdir, "bumped", _panel_src("bumped", version=4))
    reg = PanelRegistry()
    reg.discover(tmp_path)
    assert reg.resolve("bumped").version == 4


def test_start_panels_have_valid_version():
    reg = _registry()
    for dom in _EXPECTED:
        assert reg.resolve(dom).version >= 1  # migration guard: no stale/invalid version


# ── versions() accessor (audit/manifest input, 1d) ───────────────────────────────────────────────
def test_versions_roundtrip(tmp_path):
    pdir = tmp_path / "panels"
    _write(pdir, "a", _panel_src("alpha", version=2))
    _write(pdir, "b", _panel_src("beta", version=7))
    reg = PanelRegistry()
    reg.discover(tmp_path)
    assert reg.versions() == {"alpha": 2, "beta": 7}


def test_versions_for_start_panels():
    versions = _registry().versions()
    assert _EXPECTED.issubset(set(versions))
    assert all(isinstance(v, int) and v >= 1 for v in versions.values())


# ── versioning invariant: duplicate domain stays fail-loud ───────────────────────────────────────
def test_duplicate_domain_still_fail_loud_across_versions(tmp_path):
    pdir = tmp_path / "panels"
    _write(pdir, "v1", _panel_src("same-domain", version=1))
    _write(pdir, "v2", _panel_src("same-domain", version=2))  # same domain, different version/file
    reg = PanelRegistry()
    with pytest.raises(DuplicatePanelError, match="already registered"):
        reg.discover(tmp_path)
