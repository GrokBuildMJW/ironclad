"""The four start-panels (skills/mpr/panels/*.py) — Spec 05 §7 / §10.

Loads the real panel files through PanelRegistry.discover and asserts the declared content + the
resolved sovereignty: architecture-decision is fully local-only (internal), competitive is fully
offloadable (external), and the risk-assessment technical role's per-role override beats the mixed
default. Also: every role has a non-empty lens, the expected domains are present, and panel files are
pure data (a CASE with the mpr.panel.<domain> capability and NO run).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from mpr.registry.loader import PanelRegistry
from mpr.registry.resolve import resolve_policy
from mpr.registry.schema import ProviderPolicy

_MPR_ROOT = Path(__file__).resolve().parents[1]      # skills/mpr
_PANELS_DIR = _MPR_ROOT / "panels"
_EXPECTED = {"architecture-decision", "regulatory", "competitive", "risk-assessment"}


def _registry() -> PanelRegistry:
    reg = PanelRegistry()
    reg.discover(_MPR_ROOT)  # plugin root → scans skills/mpr/panels
    return reg


# ── discovery + declared content ─────────────────────────────────────────────────────────────────
def test_all_start_panels_validate():
    added = PanelRegistry().discover(_MPR_ROOT)
    assert len(added) == 4  # the four start-panels load + validate


def test_start_panels_have_expected_domains():
    assert _EXPECTED.issubset(set(_registry().domains()))


def test_each_role_has_nonempty_lens_prompt():
    reg = _registry()
    for dom in _EXPECTED:
        panel = reg.resolve(dom)
        assert panel is not None
        assert panel.roles  # non-empty panel
        for role in panel.roles:
            assert role.lens_prompt.strip(), f"{dom}/{role.role} has an empty lens_prompt"


def test_risk_assessment_slug_is_binding():
    # Spec 05 §7.4: 'risk' would miss; the declared panel must be 'risk-assessment'.
    reg = _registry()
    assert reg.resolve("risk-assessment") is not None
    assert reg.resolve("risk") is None


# ── resolved sovereignty (resolve_policy) ────────────────────────────────────────────────────────
def test_architecture_panel_is_local_only():
    panel = _registry().resolve("architecture-decision")
    assert all(resolve_policy(panel, r) == ProviderPolicy.LOCAL_ONLY for r in panel.roles)


def test_competitive_roles_are_offloadable():
    panel = _registry().resolve("competitive")
    assert all(resolve_policy(panel, r) == ProviderPolicy.OFFLOADABLE for r in panel.roles)


def test_risk_technical_role_is_local_only_override():
    panel = _registry().resolve("risk-assessment")  # mixed → default offloadable
    by_role = {r.role: resolve_policy(panel, r) for r in panel.roles}
    assert by_role["Technisch"] == ProviderPolicy.LOCAL_ONLY      # per-role override
    assert by_role["Operativ"] == ProviderPolicy.OFFLOADABLE      # inherits mixed default
    # the override genuinely matters: not all roles are local-only here.
    assert ProviderPolicy.OFFLOADABLE in by_role.values()


# ── panel files are pure data ────────────────────────────────────────────────────────────────────
def _load(path: Path):
    spec = importlib.util.spec_from_file_location(f"_paneltest_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_panel_case_has_no_run_and_namespaced_capability():
    files = sorted(p for p in _PANELS_DIR.glob("*.py") if not p.stem.startswith("_"))
    assert len(files) == 4
    for f in files:
        mod = _load(f)
        assert not hasattr(mod, "run"), f"{f.name} must not define run() — panels are data"
        case = getattr(mod, "CASE")
        assert case["capability"] == f"mpr.panel.{case['domain']}"
        assert "panel" not in case  # CASE carries only name/capability/domain/description
        assert case["description"] == mod.PANEL.description
