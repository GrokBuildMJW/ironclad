"""Export secret-gate fail-closed decision (#196, ADR-0007), offline.

The export's secret scan must never pass 'degraded' in CI/release: when a scanner is required, an
unavailable gitleaks is fail-closed. Pure decision, unit-tested. Lives in `scripts/ci/` (private) ->
skips in an installed/clean-room tree.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_EC = _REPO / "scripts" / "ci" / "export_core.py"

pytestmark = pytest.mark.skipif(
    not _EC.is_file(),
    reason="private CI export_core.py absent — installed/clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_exportcore", _EC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_unavailable_scanner_fails_closed_only_when_required():
    ec = _load()
    assert ec.secret_scan_fails_closed("unavailable", require=True) is True    # CI/release: fail-closed
    assert ec.secret_scan_fails_closed("unavailable", require=False) is False  # local dev: tolerated
    assert ec.secret_scan_fails_closed("clean", require=True) is False         # scanner ran, clean
    assert ec.secret_scan_fails_closed("leaks", require=True) is False         # handled earlier (hard fail there)


def test_require_scanner_env_default(monkeypatch):
    ec = _load()
    monkeypatch.delenv("EXPORT_REQUIRE_SCANNER", raising=False)
    assert ec._require_scanner_default() is False
    for v in ("1", "true", "YES"):
        monkeypatch.setenv("EXPORT_REQUIRE_SCANNER", v)
        assert ec._require_scanner_default() is True
    monkeypatch.setenv("EXPORT_REQUIRE_SCANNER", "0")
    assert ec._require_scanner_default() is False
