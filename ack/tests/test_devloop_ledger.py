"""Append-only transition ledger + kill-switch (epic #262, S13 / ADR 0002), offline.

Lives in `scripts/devloop/` (private) -> skips in an installed/clean-room tree. Pins that the
hash-chain links records, that `verify_chain` actually catches a tampered payload AND a
truncated/reordered chain (the tamper-evidence the reconciler relies on), and the kill-switch
sentinel round-trips.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_LEDGER = _REPO / "scripts" / "devloop" / "ledger.py"

pytestmark = pytest.mark.skipif(
    not _LEDGER.is_file(),
    reason="private dev-loop ledger (scripts/devloop/ledger.py) absent — clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_devloop_ledger", _LEDGER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_append_links_the_chain_and_verifies(tmp_path):
    lg = _load()
    f = tmp_path / "ledger.jsonl"
    a = lg.append(f, {"unit": 1, "src": "READY", "dst": "BRANCH"})
    b = lg.append(f, {"unit": 1, "src": "BRANCH", "dst": "IMPLEMENT"})
    assert a["seq"] == 0 and b["seq"] == 1
    assert b["prev_hash"] == a["hash"]                 # chained
    assert lg.read_all(f) and lg.verify_chain(f) == []  # intact


def test_verify_catches_a_tampered_payload(tmp_path):
    lg = _load()
    f = tmp_path / "ledger.jsonl"
    lg.append(f, {"v": "original"}); lg.append(f, {"v": "second"})
    lines = f.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace('"original"', '"FORGED"')   # change payload, keep the stored hash
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    errs = lg.verify_chain(f)
    assert any("hash mismatch" in e for e in errs)


def test_verify_catches_truncation(tmp_path):
    lg = _load()
    f = tmp_path / "ledger.jsonl"
    lg.append(f, {"v": 1}); lg.append(f, {"v": 2})
    lines = f.read_text(encoding="utf-8").splitlines()
    f.write_text(lines[1] + "\n", encoding="utf-8")         # drop the first record
    errs = lg.verify_chain(f)
    assert errs                                              # seq gap + prev_hash break


def test_kill_switch_round_trip(tmp_path):
    lg = _load()
    sw = tmp_path / "KILL"
    assert not lg.kill_switch_engaged(sw)
    lg.engage_kill_switch(sw, "operator halt")
    assert lg.kill_switch_engaged(sw)
    lg.disengage_kill_switch(sw)
    assert not lg.kill_switch_engaged(sw)
