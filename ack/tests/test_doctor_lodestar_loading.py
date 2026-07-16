"""#1532: the doctor must FAIL CLOSED when Lodestar is requested but its plugin cannot be loaded.

`_load_lodestar_checks(True)` used to swallow every import/init failure into an empty list —
indistinguishable from Lodestar being *disabled* — so `python -m ack.doctor --lodestar` (or the runtime
`server._doctor_report` under `LODESTAR_ENABLED`) could run only the generic checks and report success
(exit 0) while the capability/gap-tracking checks silently never ran. It now returns a check that emits a
visible ERROR Finding instead.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_CORE = Path(__file__).resolve().parents[2]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from ack import doctor  # noqa: E402


def test_disabled_returns_no_checks():
    # not requested → empty (a legitimate, silent no-op — the only case where [] is correct)
    assert doctor._load_lodestar_checks(False) == []


def test_import_failure_fails_closed_with_an_error(monkeypatch):
    # simulate a packaging omission / broken install: force the from-import to raise ImportError
    monkeypatch.setitem(sys.modules, "ack.lodestar.doctor_checks", None)
    checks = doctor._load_lodestar_checks(True)
    assert checks, "a requested-but-unloadable Lodestar must not collapse to an empty (disabled-looking) list"
    findings = []
    for c in checks:
        findings.extend(c(doctor.DoctorContext(root=Path("."))))
    errs = [f for f in findings if f.severity is doctor.Severity.ERROR and f.check == "lodestar"]
    assert errs, "the load failure must surface as a lodestar ERROR finding (fail closed)"
    assert "failed to load" in errs[0].message


def test_enabled_and_loadable_returns_the_real_checks():
    # the norm: the plugin imports → the real ordered Lodestar check set (several), NOT the single
    # synthetic error stub the fail-closed path returns.
    checks = doctor._load_lodestar_checks(True)
    assert len(checks) > 1, "enabled + loadable returns Lodestar's real checks, not the 1-element error stub"
