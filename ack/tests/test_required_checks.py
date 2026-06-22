"""Required-status-checks SSOT ⟺ workflow jobs (#214, ADR-0007), offline.

Pure logic (matrix-name expansion + SSOT validation). Lives in `scripts/ci/` (private) → skips in an
installed/clean-room tree. Also pins the live SSOT against the real exported workflows.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_CRC = _REPO / "scripts" / "ci" / "check_required_checks.py"

pytestmark = pytest.mark.skipif(
    not _CRC.is_file(),
    reason="private CI check_required_checks.py absent — installed/clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_crc", _CRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_matrix_name_expansion():
    crc = _load()
    assert crc.expand_job_check_names("test", {"strategy": {"matrix": {"python-version": ["3.10", "3.12"]}}}) == \
        ["test (3.10)", "test (3.12)"]
    assert crc.expand_job_check_names("secret-scan", {}) == ["secret-scan"]
    assert crc.expand_job_check_names("x", {"name": "Pretty"}) == ["Pretty"]   # display name wins


def test_validate_passes_when_names_map_to_jobs():
    crc = _load()
    workflows = {"ci.yml": {"jobs": {"test": {"strategy": {"matrix": {"python-version": ["3.10", "3.12"]}}},
                                     "secret-scan": {}}}}
    entries = [
        {"name": "test (3.10)", "workflow": "ci.yml", "job": "test"},
        {"name": "secret-scan", "workflow": "ci.yml", "job": "secret-scan"},
    ]
    assert crc.validate_ssot_jobs(entries, workflows) == []


def test_validate_flags_missing_job_and_matrix_drift():
    crc = _load()
    workflows = {"ci.yml": {"jobs": {"test": {"strategy": {"matrix": {"python-version": ["3.10"]}}}}}}
    entries = [
        {"name": "test (3.12)", "workflow": "ci.yml", "job": "test"},        # matrix only has 3.10
        {"name": "ghost", "workflow": "ci.yml", "job": "does-not-exist"},     # no such job
        {"name": "x", "workflow": "missing.yml", "job": "y"},                 # no such workflow
    ]
    drift = crc.validate_ssot_jobs(entries, workflows)
    assert any("test (3.12)" in d and "drift" in d for d in drift)
    assert any("does-not-exist" in d for d in drift)
    assert any("missing.yml" in d for d in drift)
    assert len(drift) == 3


def test_required_for_skips_convention_only():
    crc = _load()
    ssot = {"repos": {"pub": {"required": [{"name": "a"}]}, "priv": {"convention_only": True, "required": []}}}
    assert len(crc.required_for(ssot, "pub")) == 1
    assert crc.required_for(ssot, "priv") == []


def test_live_ssot_maps_to_real_exported_jobs():
    crc = _load()
    import yaml
    ssot = yaml.safe_load((_REPO / ".github" / "required-status-checks.yml").read_text(encoding="utf-8"))
    entries = crc.required_for(ssot, crc.EXPORT_REPO)
    assert entries, "SSOT must list required checks for the export repo"
    workflows = crc._load_workflows({e["workflow"] for e in entries})
    assert crc.validate_ssot_jobs(entries, workflows) == [], "SSOT names must map to real exported jobs"
    assert any(e["name"] == "secret-scan" for e in entries), "secret-scan must be a required check (#196 gap)"
