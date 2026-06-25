"""DELIVERED-PENDING async completion watcher (#348 S12), offline.

Lives in `scripts/devloop/` (private) -> skips in an installed/clean-room tree. Pins the watcher
adjudication (pending / yank-candidate / done), the published-from-ledger source that feeds
`resume.already_published`, and that a pending/yank delivery is NOT counted published.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_DEV = _REPO / "scripts" / "devloop"

pytestmark = pytest.mark.skipif(
    not (_DEV / "watcher.py").is_file(),
    reason="private dev-loop watcher (scripts/devloop/watcher.py) absent — clean-room tree",
)


def _load(stem):
    spec = importlib.util.spec_from_file_location(f"_devloop_{stem}", _DEV / f"{stem}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_watch_delivery_pending_until_green_smoke_and_closed_roundtrip():
    w = _load("watcher")
    # smoke not concluded -> delivered-pending
    assert w.watch_delivery(smoke_conclusion=None, roundtrip_done=True)["status"] == "delivered-pending"
    assert w.watch_delivery(smoke_conclusion="", roundtrip_done=True)["status"] == "delivered-pending"
    # smoke green but round-trip not closed out -> still delivered-pending
    r = w.watch_delivery(smoke_conclusion="success", roundtrip_done=False, roundtrip_reasons=["ironclad#7 pending"])
    assert r["state"] == "DELIVER" and r["status"] == "delivered-pending" and any("#7" in x for x in r["reasons"])
    # smoke green AND round-trip done -> DELIVERED
    done = w.watch_delivery(smoke_conclusion="success", roundtrip_done=True)
    assert done["state"] == "DELIVERED" and done["status"] == "delivered"


def test_watch_delivery_red_smoke_is_a_yank_candidate_not_done():
    w = _load("watcher")
    for concl in ("failure", "timed_out", "startup_failure", "cancelled"):
        r = w.watch_delivery(smoke_conclusion=concl, roundtrip_done=True)
        assert r["state"] == "DELIVER" and r["status"] == "delivered-yank-candidate"
        assert any("yank" in x for x in r["reasons"])             # surfaced, never auto-yanked


def test_published_issues_from_ledger_counts_only_terminal_delivered():
    w = _load("watcher")
    records = [
        {"seq": 0, "payload": {"surface": "DELIVER", "status": "delivered", "release_index": "pypi", "unit": 358}},
        {"seq": 1, "payload": {"surface": "DELIVER", "status": "delivered-pending", "release_index": "pypi", "unit": 359}},
        {"seq": 2, "payload": {"surface": "DELIVER", "status": "delivered-yank-candidate", "release_index": "pypi", "unit": 360}},
        {"seq": 3, "payload": {"surface": "DRIVER", "status": "delivered", "release_index": "pypi", "unit": 999}},   # not a DELIVER record
        {"seq": 4, "payload": {"surface": "DELIVER", "status": "delivered", "release_index": "pypi", "unit": 358}},  # dedup
        {"seq": 5, "payload": {"surface": "DELIVER", "status": "delivered", "release_index": "testpypi", "unit": 361}},  # #433: Test-PyPI proof, NOT published
    ]
    assert w.published_issues_from_ledger(records) == [358]        # pending / yank / non-DELIVER / Test-PyPI excluded


def test_testpypi_delivered_is_not_published_433():
    # #433: a Test-PyPI terminal `delivered` record (an isolated proof) must NOT count as published —
    # else the production `--complete-delivery` idempotency short-circuits and never appends the
    # production terminal record (the bug the first real production cut surfaced).
    w = _load("watcher")
    records = [{"seq": 0, "payload": {"surface": "DELIVER", "status": "delivered", "release_index": "testpypi", "unit": 348}}]
    assert w.published_issues_from_ledger(records) == []           # testpypi proof ≠ public delivery
    records.append({"seq": 1, "payload": {"surface": "DELIVER", "status": "delivered", "release_index": "pypi", "unit": 348}})
    assert w.published_issues_from_ledger(records) == [348]        # the production delivery DOES count


def test_published_source_feeds_resume_already_published():
    w = _load("watcher")
    resume = _load("resume")
    records = [{"seq": 0, "payload": {"surface": "DELIVER", "status": "delivered", "release_index": "pypi", "unit": 358}}]
    published = w.published_issues_from_ledger(records)
    assert resume.already_published(358, published) is True       # terminal: never re-delivered
    assert resume.already_published(359, published) is False      # a different (unpublished) unit
