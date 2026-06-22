"""doc-reality-audit: per-doc responsibility lint (#174, ADR-0006).

Pins the structural anti-drift guard: `roadmap.md` must be future-only (no realized markers) and
`status.md` must be now-only (no future markers). Includes the **negative test** — a deliberately
"realized" roadmap item must make the audit FAIL — proving the guard actually bites, plus a positive
test that the REAL shipped docs pass.

`doc_reality_audit.py` lives in `scripts/ci/` (private, not exported), so this **skips** in an
installed/clean-room tree where it is absent (mirrors `test_export_leak_guard.py`).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]                 # mjw_agentic/
_AUDIT = _REPO / "scripts" / "ci" / "doc_reality_audit.py"
_CORE = _REPO / "core"

pytestmark = pytest.mark.skipif(
    not _AUDIT.is_file(),
    reason="private CI audit (scripts/ci/doc_reality_audit.py) absent — installed/clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_audit_mod", _AUDIT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mkdocs(tmp_path: Path, roadmap: str, status: str = "# Status\n\nversion 0.0.x runs now.\n") -> Path:
    d = tmp_path / "docs"
    d.mkdir(parents=True, exist_ok=True)
    (d / "roadmap.md").write_text(roadmap, encoding="utf-8")
    (d / "status.md").write_text(status, encoding="utf-8")
    return tmp_path


def test_real_docs_pass_the_responsibility_lint():
    # the actually-shipped roadmap.md + status.md must be clean (regression on the real tree)
    audit = _load()
    findings = audit.check_doc_responsibilities(_CORE)
    assert findings == [], f"real docs tripped the responsibility lint: {findings}"


def test_realized_marker_in_roadmap_fails(tmp_path):
    audit = _load()
    root = _mkdocs(tmp_path, "# Roadmap\n\n## Some theme\n\n- This feature **shipped** in v0.0.x.\n")
    findings = audit.check_doc_responsibilities(root)
    assert findings and any("roadmap.md" in f and "shipped" in f for f in findings)


@pytest.mark.parametrize("marker", ["shipped", "delivered", "wired + tested", "now available"])
def test_each_realized_marker_is_caught(tmp_path, marker):
    audit = _load()
    root = _mkdocs(tmp_path, f"# Roadmap\n\n## Theme\n\n- It is {marker} already.\n")
    assert audit.check_doc_responsibilities(root), f"{marker!r} not caught in roadmap"


def test_future_marker_in_status_fails(tmp_path):
    audit = _load()
    root = _mkdocs(tmp_path, "# Roadmap\n\n## Theme\n\n- planned work.\n",
                   status="# Status\n\nversion 0.0.x. Feature X is coming soon.\n")
    findings = audit.check_doc_responsibilities(root)
    assert findings and any("status.md" in f and "coming soon" in f for f in findings)


def test_clean_future_only_roadmap_passes(tmp_path):
    audit = _load()
    root = _mkdocs(tmp_path, "# Roadmap\n\n## Theme\n\n- We plan to build X; it will support Y.\n")
    assert audit.check_doc_responsibilities(root) == []


def test_roadmap_pointer_in_status_is_not_flagged(tmp_path):
    # a legitimate "see the roadmap" pointer in status.md must NOT trip the lint
    audit = _load()
    root = _mkdocs(tmp_path, "# Roadmap\n\n## Theme\n\n- future work.\n",
                   status="# Status\n\nversion 0.0.x. No multi-user auth yet; see the roadmap.\n")
    assert audit.check_doc_responsibilities(root) == []
