"""Export-leak guard (#137): the internal plugin repo can never enter `core/` or the public export.

`core/` couples to plugins ONLY via the generic `ironclad.plugins` entry-point group (#136) — never
by the concrete private repo name. This pins that guarantee: the boundary + export forbidden-literal
lists cover that repo name, the guards actually flag a synthetic leak, and the real `core/` +
`clients/ink` tree is clean.

NOTE: the private repo name is assembled by concatenation (`_INTERNAL_REPO`) so this test file does
**not** itself contain the contiguous literal — otherwise it would (correctly) trip the very guard
it verifies. These guards live in `scripts/ci/` (private, not exported), so the test **skips** when
run from an installed/clean-room tree where `scripts/ci/` is absent.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]          # the monorepo root
_BOUNDARY = _REPO / "scripts" / "ci" / "check_core_boundary.py"
_EXPORT = _REPO / "scripts" / "ci" / "export_core.py"
# Assembled so the contiguous literal never appears in this source file (see module docstring).
_INTERNAL_REPO = "ironclad-plugins-" + "internal"

pytestmark = pytest.mark.skipif(
    not (_BOUNDARY.is_file() and _EXPORT.is_file()),
    reason="private CI guards (scripts/ci/) absent — installed/clean-room tree, guard not applicable",
)


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(f"_guard_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_internal_repo_is_a_forbidden_literal_in_boundary():
    cb = _load(_BOUNDARY)
    assert any(p.search(_INTERNAL_REPO) for p in cb.FORBIDDEN_LITERALS), \
        "boundary check does not forbid the internal plugin repo name"


def test_internal_repo_is_a_forbidden_pattern_in_export():
    ex = _load(_EXPORT)
    assert any(p.search(_INTERNAL_REPO) for _rule, p in ex._PATTERNS), \
        "export secret-sweep does not forbid the internal plugin repo name"


def test_boundary_flags_a_synthetic_leak(tmp_path):
    cb = _load(_BOUNDARY)
    leaky = tmp_path / "leak.py"
    leaky.write_text(f'PLUGIN_REPO = "GrokBuildMJW/{_INTERNAL_REPO}"\n', encoding="utf-8")
    violations: list[str] = []
    cb._check_literals(leaky, violations)
    assert violations, "the boundary literal sweep failed to flag the internal repo name"


def test_real_core_and_client_tree_is_clean():
    # the actual guarantee: nothing under core/ or clients/ink names the internal plugin repo
    cb = _load(_BOUNDARY)
    pat = next(p for p in cb.FORBIDDEN_LITERALS if p.search(_INTERNAL_REPO))
    hits: list[str] = []
    for base in (_REPO / "core", _REPO / "clients" / "ink"):
        if not base.is_dir():
            continue
        for f in base.rglob("*"):
            if not f.is_file() or f.suffix.lower() not in {
                ".py", ".md", ".json", ".yml", ".yaml", ".toml", ".ts", ".tsx", ".txt", ".cfg", ".ini"
            }:
                continue
            if any(part in {"__pycache__", "node_modules", ".venv", "dist", "build"} for part in f.parts):
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if pat.search(text):
                hits.append(str(f.relative_to(_REPO)))
    assert not hits, f"internal plugin repo name leaked into the export surface: {hits}"
