"""DOCTOR-VAL (#503): `--validate-tasks` validates every STORED task record against the live TaskSpec,
not just the canonical EXAMPLE_TASK_JSON — so contract drift in a real shipped task is caught.

The flag was parsed into `DoctorContext.validate_tasks` but no check ever read it; `check_task_records_validate`
now honors it (no-op when off; never raises — a malformed task is an `err` finding).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_CORE = Path(__file__).resolve().parents[2]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from ack import case_spec, doctor  # noqa: E402
from ack.lodestar.spec import CapabilityTaskSpec  # noqa: E402

_VALID = dict(case_spec.EXAMPLE_TASK_JSON)     # the canonical example validates against TaskSpec
_INVALID: dict = {"task_id": "broken"}          # missing required TaskSpec fields → fails validation


def _rec(tid: str, data: dict) -> "doctor.TaskRecord":
    return doctor.TaskRecord(tid, "open", Path(f"tasks/open/{tid}.json"), data)


def _ctx(records, validate, capability_spec_cls=None):
    ctx = doctor.DoctorContext(root=Path("."), validate_tasks=validate,
                               capability_spec_cls=capability_spec_cls)
    ctx.case_spec = case_spec
    ctx.records = records
    return ctx


def test_validate_tasks_off_is_a_noop():
    # disabled (the default) → no findings even with a malformed record (byte-identical to before)
    assert doctor.check_task_records_validate(_ctx([_rec("t1", _INVALID)], validate=False)) == []


def test_validate_tasks_flags_a_bad_record_only():
    out = doctor.check_task_records_validate(_ctx([_rec("good", _VALID), _rec("bad", _INVALID)], validate=True))
    errs = [f for f in out if f.severity is doctor.Severity.ERROR]
    assert any("bad" in f.message for f in errs)          # the malformed record is flagged
    assert all("good" not in f.message for f in errs)     # the valid record is not


def test_validate_tasks_all_good_reports_ok_and_never_raises():
    out = doctor.check_task_records_validate(_ctx([_rec("g1", _VALID), _rec("g2", _VALID)], validate=True))
    assert [f for f in out if f.severity is doctor.Severity.ERROR] == []   # all valid → no errors
    assert out and any("validate against TaskSpec" in f.message for f in out)   # a positive finding


def test_validate_tasks_flags_capability_record_when_lodestar_off():
    # #1531: with Lodestar OFF (no capability_spec_cls) a capability record validates against the BASE
    # TaskSpec — extra='forbid' + the missing required fields make `{"capability":"x"}` a task-validate
    # ERROR. Previously the record was silently SKIPPED, so --validate-tasks reported success on a
    # malformed task, defeating the gate.
    out = doctor.check_task_records_validate(_ctx([_rec("CAP-1", {"capability": "x"})], validate=True))
    errs = [f for f in out if f.severity is doctor.Severity.ERROR]
    assert any("CAP-1" in f.message for f in errs)


def test_validate_tasks_capability_record_uses_capability_spec_when_lodestar_on():
    # #1531: with Lodestar ON the capability record validates against CapabilityTaskSpec — a well-formed
    # capability task PASSES while a malformed one (missing base fields) is still flagged.
    good = {**_VALID, "capability": "demo-cap"}
    out = doctor.check_task_records_validate(
        _ctx([_rec("CAP-GOOD", good), _rec("CAP-BAD", {"capability": "x"})],
             validate=True, capability_spec_cls=CapabilityTaskSpec))
    errs = [f for f in out if f.severity is doctor.Severity.ERROR]
    assert any("CAP-BAD" in f.message for f in errs)        # malformed capability task flagged
    assert all("CAP-GOOD" not in f.message for f in errs)   # well-formed capability task passes
