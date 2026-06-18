"""F-4: the shipped demo-vessel is valid / runnable.

The demo-vessel (examples/demo-vessel/) is part of the public export. This pins that it
**passes the doctor** (ACK-kernel schema, TaskStore integrity, deps, and the Lodestar
capability/gap-tracking checks) with **no errors** — so a user who runs it from the export
gets a working example, not a broken one. Warnings (e.g. optional .mcp.json) are allowed.
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

_DEMO = _CORE / "examples" / "demo-vessel"


def test_demo_vessel_passes_doctor():
    assert _DEMO.is_dir(), "demo-vessel must ship in the export"
    report = doctor.run_doctor(
        _DEMO,
        extra_checks=doctor._load_lodestar_checks(True),   # capability/gap-tracking checks
        validate_tasks=True,
        include_done=True,
    )
    errors = [f for f in report.findings if f.severity is doctor.Severity.ERROR]
    assert not errors, "demo-vessel has doctor ERROR(s): " + \
        "; ".join(f"{f.check}: {f.message}" for f in errors)
    # sanity: the doctor actually ran real checks (not an empty/skipped pass)
    assert report.count(doctor.Severity.OK) > 0
