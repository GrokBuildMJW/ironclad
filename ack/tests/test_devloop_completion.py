"""Machine epic-completion trigger — epic-bundled delivery (#348 S13), offline.

Lives in `scripts/devloop/` (private) -> skips in an installed/clean-room tree. Pins the FAIL-CLOSED
trigger: a complete + C2-accepted epic is ready; an unmerged unit, a blocked unit, an empty epic, or
C2-not-accepted leaves the bundle UNdelivered (the mandated negative test).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_C = _REPO / "scripts" / "devloop" / "completion.py"

pytestmark = pytest.mark.skipif(
    not _C.is_file(),
    reason="private dev-loop completion (scripts/devloop/completion.py) absent — clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_devloop_completion", _C)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _u(number, merged=True, *labels):
    return {"number": number, "merged": merged, "labels": list(labels)}


def test_complete_c2_accepted_epic_is_ready():
    c = _load()
    units = [_u(350), _u(351), _u(352)]
    ready, reasons = c.epic_ready_for_delivery(units, c2_accepted=True)
    assert ready and reasons == []


def test_one_unmerged_unit_blocks_the_bundle_delivery():
    c = _load()
    units = [_u(350), _u(351, merged=False), _u(352)]          # #351 not merged
    ready, reasons = c.epic_ready_for_delivery(units, c2_accepted=True)
    assert not ready and any("#351 not merged" in r for r in reasons)     # the mandated negative test


def test_blocked_unit_and_empty_epic_and_no_c2_all_fail_closed():
    c = _load()
    # a blocked (or in-review / needs-decision) unit holds the bundle
    ready, reasons = c.epic_ready_for_delivery([_u(350), _u(351, True, "status/blocked")], c2_accepted=True)
    assert not ready and any("status/blocked" in r for r in reasons)
    # an empty epic never auto-delivers
    ready, reasons = c.epic_ready_for_delivery([], c2_accepted=True)
    assert not ready and any("no native sub-issues" in r for r in reasons)
    # C2 not accepted holds an otherwise-complete epic
    ready, reasons = c.epic_ready_for_delivery([_u(350)], c2_accepted=False)
    assert not ready and any("C2 not accepted" in r for r in reasons)


def test_blocking_set_is_the_shared_ssot():
    # #361 S11 SSOT reused: in-review (a SELECT-blocking label) also holds bundle delivery
    c = _load()
    ready, reasons = c.epic_ready_for_delivery([_u(350, True, "status/in-review")], c2_accepted=True)
    assert not ready and any("status/in-review" in r for r in reasons)
