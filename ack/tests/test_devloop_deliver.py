"""GO-gated DELIVER credential lane (epic #348 S7 / ADR 0002), offline.

Lives in `scripts/devloop/` (private) -> skips in an installed/clean-room tree. Pins the irreversible
public action behind a tree/version-bound, SINGLE-USE operator GO: a valid GO authorizes + is consumed
(replay refused); absent/forged/wrong-operator/wrong-tree/wrong-version GOs are refused and consume
NOTHING; and `execute_delivery` is a no-op refusal without authorization + never auto.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_DELIVER = _REPO / "scripts" / "devloop" / "deliver.py"

pytestmark = pytest.mark.skipif(
    not _DELIVER.is_file(),
    reason="private dev-loop deliver lane (scripts/devloop/deliver.py) absent — clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_devloop_deliver", _DELIVER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_SECRET = b"go-secret"


def test_authorize_delivery_valid_bound_then_single_use(tmp_path):
    d = _load()
    lp = tmp_path / "ledger.jsonl"
    go = d.dial.compute_go(356, "DELIVER", "alice", _SECRET, tree_sha="abc123", version="0.0.16")
    common = dict(unit=356, operator="alice", secret=_SECRET, tree_sha="abc123", version="0.0.16", ledger_path=lp)
    ok, _ = d.authorize_delivery(go=go, **common)
    assert ok
    assert any((r["payload"].get("go_consumed") == go) for r in d.ledger.read_all(lp))   # consumed = recorded
    # REPLAY of the same GO is refused (single-use, replay-proof — even spanning push->release)
    again, why = d.authorize_delivery(go=go, **common)
    assert not again and "replay" in why


def test_authorize_delivery_refuses_and_consumes_nothing_for_bad_gos(tmp_path):
    d = _load()
    lp = tmp_path / "l.jsonl"
    good = dict(unit=356, operator="alice", secret=_SECRET, tree_sha="abc", version="1", ledger_path=lp)
    assert not d.authorize_delivery(go=None, **good)[0]                                   # absent -> parked
    assert not d.authorize_delivery(go="deadbeef", **good)[0]                             # forged
    other_tree = d.dial.compute_go(356, "DELIVER", "alice", _SECRET, tree_sha="OTHER", version="1")
    assert not d.authorize_delivery(go=other_tree, **good)[0]                             # wrong tree_sha
    other_ver = d.dial.compute_go(356, "DELIVER", "alice", _SECRET, tree_sha="abc", version="9")
    assert not d.authorize_delivery(go=other_ver, **good)[0]                              # wrong version
    mallory = d.dial.compute_go(356, "DELIVER", "mallory", _SECRET, tree_sha="abc", version="1")
    assert not d.authorize_delivery(go=mallory, **good)[0]                                # wrong operator
    assert d.ledger.read_all(lp) == []                                                    # nothing consumed


def test_execute_delivery_is_gated_and_never_auto(tmp_path):
    d = _load()
    calls: list = []

    def runner(name, argv):
        calls.append(name)
        return (0, "ok")

    cmds = d.delivery_commands(tmp_path, "v0.0.16", "owner/ironclad")
    # unauthorized -> NO-OP refusal: nothing is pushed/published
    ok, log = d.execute_delivery(False, "no GO", cmds, runner)
    assert not ok and calls == [] and "nothing executed" in log[0]
    # authorized -> the two irreversible steps run in order
    ok2, _ = d.execute_delivery(True, "GO ok", cmds, runner)
    assert ok2 and calls == ["mirror-push", "release-create"]


def test_execute_delivery_halts_fail_closed_on_a_red_step(tmp_path):
    d = _load()

    def runner(name, argv):
        return (1, "boom") if name == "mirror-push" else (0, "")

    cmds = d.delivery_commands(tmp_path, "v1", "owner/r")
    ok, log = d.execute_delivery(True, "GO ok", cmds, runner)
    assert not ok and any("mirror-push FAILED" in x for x in log)


def test_delivery_commands_are_the_push_and_release_create(tmp_path):
    d = _load()
    cmds = dict(d.delivery_commands(tmp_path, "v0.0.16", "owner/ironclad"))
    assert cmds["mirror-push"][0] == "bash" and cmds["mirror-push"][1].endswith("publish_core.sh")
    assert cmds["mirror-push"][-1] == "--push"
    assert cmds["release-create"][:3] == ["gh", "release", "create"] and "v0.0.16" in cmds["release-create"]
    assert "owner/ironclad" in cmds["release-create"]


# ── #432: the GitHub release body is the CHANGELOG section, not a placeholder ──
def _write_changelog(root: Path, body: str) -> None:
    (root / "core").mkdir(parents=True, exist_ok=True)
    (root / "core" / "CHANGELOG.md").write_text(body, encoding="utf-8")


_CHANGELOG = """# Changelog

## [Unreleased]

## [0.0.16] - 2026-06-20
### Fixed
- **Real fix one** (#100): something meaningful happened here.
- **Real fix two** (#101): and another.

## [0.0.15] - 2026-06-19
### Added
- An older entry that must NOT leak into the 0.0.16 notes.
"""


def test_changelog_notes_extracts_the_section_body(tmp_path):
    d = _load()
    _write_changelog(tmp_path, _CHANGELOG)
    notes = d.changelog_notes(tmp_path, "v0.0.16")
    assert notes is not None
    assert notes.startswith("### Fixed")                       # heading line stripped, body kept
    assert "Real fix one" in notes and "Real fix two" in notes
    assert "0.0.15" not in notes and "must NOT leak" not in notes  # stops at the next '## ' heading


def test_changelog_notes_strips_v_prefix(tmp_path):
    d = _load()
    _write_changelog(tmp_path, _CHANGELOG)
    assert d.changelog_notes(tmp_path, "0.0.16") == d.changelog_notes(tmp_path, "v0.0.16")
    assert d.changelog_notes(tmp_path, "v0.0.16") is not None


def test_changelog_notes_fail_soft(tmp_path):
    d = _load()
    # no CHANGELOG file at all
    assert d.changelog_notes(tmp_path, "v0.0.16") is None
    # file present but the version has no section
    _write_changelog(tmp_path, _CHANGELOG)
    assert d.changelog_notes(tmp_path, "v9.9.9") is None
    # empty tag
    assert d.changelog_notes(tmp_path, "") is None
    # a section with a heading but no list entry -> None (placeholder is more honest)
    _write_changelog(tmp_path, "# Changelog\n\n## [0.0.99] - 2026-06-24\n\nProse only, no bullet.\n")
    assert d.changelog_notes(tmp_path, "v0.0.99") is None


def test_delivery_commands_uses_changelog_notes_when_present(tmp_path):
    d = _load()
    _write_changelog(tmp_path, _CHANGELOG)
    cmds = dict(d.delivery_commands(tmp_path, "v0.0.16", "owner/ironclad"))
    rc = cmds["release-create"]
    notes = rc[rc.index("--notes") + 1]
    assert "Real fix one" in notes and "GO-gated" not in notes     # the real section, not the placeholder


def test_delivery_commands_falls_back_to_placeholder_without_changelog(tmp_path):
    d = _load()
    cmds = dict(d.delivery_commands(tmp_path, "v0.0.16", "owner/ironclad"))
    rc = cmds["release-create"]
    assert rc[rc.index("--notes") + 1] == "DELIVER v0.0.16 (engine, GO-gated)"


# ── #348 S7 review hardening: never-auto, artifact-bound, structurally GO-bound scope ──
def test_authorize_delivery_hard_forces_supervised_ignoring_auto_dial(tmp_path):
    # the irreversible publish lane NEVER auto-advances: a caller-supplied {'DELIVER':'auto'} cannot bypass
    # the GO (DELIVER 'auto' is forbidden). Without a valid GO it still refuses + consumes nothing.
    d = _load()
    lp = tmp_path / "l.jsonl"
    ok, _ = d.authorize_delivery(go=None, unit=356, operator="alice", secret=_SECRET,
                                 tree_sha="abc", version="1", ledger_path=lp, dial_config={"DELIVER": "auto"})
    assert not ok and d.ledger.read_all(lp) == []
    bad = d.authorize_delivery(go="deadbeef", unit=356, operator="alice", secret=_SECRET,
                               tree_sha="abc", version="1", ledger_path=lp, dial_config={"DELIVER": "auto"})
    assert not bad[0] and d.ledger.read_all(lp) == []


def test_authorize_delivery_requires_non_empty_tree_and_version(tmp_path):
    # must-fix #5: a DELIVER GO MUST bind a real tree + version — empties fail closed (no silent degrade).
    d = _load()
    lp = tmp_path / "l.jsonl"
    for ts, ver in [("", "1"), ("abc", ""), ("", "")]:
        go = d.dial.compute_go(356, "DELIVER", "alice", _SECRET, tree_sha=ts, version=ver)
        ok, why = d.authorize_delivery(go=go, unit=356, operator="alice", secret=_SECRET,
                                       tree_sha=ts, version=ver, ledger_path=lp)
        assert not ok and "binding incomplete" in why
    assert d.ledger.read_all(lp) == []


def test_authorize_delivery_binds_release_index(tmp_path):
    # #395 S14a (blocker D1-1): a Test-PyPI GO is cryptographically REJECTED for the production index, so
    # "Test-PyPI FIRST" is engine-enforced, not operator-discipline. The wrong-index consume records NOTHING.
    d = _load()
    testpypi = d.dial.compute_go(364, "DELIVER", "alice", _SECRET, tree_sha="abc", version="1", release_index="testpypi")
    lp = tmp_path / "ok.jsonl"
    ok, _ = d.authorize_delivery(go=testpypi, unit=364, operator="alice", secret=_SECRET, tree_sha="abc",
                                 version="1", release_index="testpypi", ledger_path=lp)
    assert ok                                                                      # right index -> authorized
    lp2 = tmp_path / "wrong.jsonl"
    no, why = d.authorize_delivery(go=testpypi, unit=364, operator="alice", secret=_SECRET, tree_sha="abc",
                                   version="1", release_index="pypi", ledger_path=lp2)
    assert not no and d.ledger.read_all(lp2) == []                                 # wrong index -> refused, nothing consumed


def test_deliver_scope_binds_the_relaxed_set_to_authorization():
    # the delivery target enters the allowed set ONLY when authorized — structural, not docstring-only.
    d = _load()
    assert d.deliver_scope(False, "owner/mono", "owner/ironclad") == ["owner/mono"]
    assert set(d.deliver_scope(True, "owner/mono", "owner/ironclad")) == {"owner/mono", "owner/ironclad"}
    # refuse_to_start on the DELIVER leg: unauthorized -> the delivery target is still refused; authorized -> ok
    assert d.credentials.refuse_to_start(["owner/ironclad"], d.deliver_scope(False, "owner/mono", "owner/ironclad"))
    assert d.credentials.refuse_to_start(["owner/ironclad"], d.deliver_scope(True, "owner/mono", "owner/ironclad")) == []


# ── #348 S8: the MERGE -> DELIVER leg (deliver_release orchestration) ──
class _Gate:
    def __init__(self, ok, reasons=()):
        self.ok = ok
        self.reasons = list(reasons)

    def __bool__(self):
        return self.ok


def _deliver_ops(d, *, gate_ok=True, authorized=True, delivered=True):
    state = {"disposed": [], "calls": [], "log": []}
    ops = d.DeliverOps(
        stage_base=lambda: "BASE",
        deliver_gate=lambda b: _Gate(gate_ok, [] if gate_ok else ["clean-room red"]),
        authorize=lambda: (state["calls"].append("authorize") or (authorized, "GO ok" if authorized else "no GO")),
        execute=lambda a, why: (state["calls"].append("execute") or (delivered, ["pushed", "released"] if delivered else ["push failed"])),
        dispose=lambda b: state["disposed"].append(b),
        log=lambda rec: state["log"].append(rec),
    )
    return ops, state


def test_deliver_release_happy_path_is_pending_not_terminal(tmp_path):
    # #396 S14b: a shipped push parks at DELIVER/delivered-pending — terminal DELIVERED comes ONLY from the
    # --complete-delivery gate (post-publish-smoke + round-trip). done-means-deployed.
    d = _load()
    ops, st = _deliver_ops(d)
    out = d.deliver_release(ops)
    assert out.state == "DELIVER" and out.status == "delivered-pending"
    assert st["calls"] == ["authorize", "execute"] and st["disposed"] == ["BASE"]   # ordered + base disposed


def test_deliver_release_red_gate_halts_before_any_go_or_push(tmp_path):
    d = _load()
    ops, st = _deliver_ops(d, gate_ok=False)
    out = d.deliver_release(ops)
    assert out.state == "DELIVER" and out.status == "halted-gate"
    assert st["calls"] == [] and st["disposed"] == ["BASE"]       # never authorized/executed; still disposed


def test_deliver_release_parks_without_a_valid_go(tmp_path):
    d = _load()
    ops, st = _deliver_ops(d, authorized=False)
    out = d.deliver_release(ops)
    assert out.status == "parked-awaiting-go"
    assert st["calls"] == ["authorize"] and "execute" not in st["calls"]   # green gate, no GO -> no push
    assert st["disposed"] == ["BASE"]                                      # base disposed on the parked path too


def test_execute_delivery_threads_target_from_pushed_sha(tmp_path):
    # #348 S8 review fix: the pushed sha is captured from mirror-push and threaded into release-create
    # --target so the tag binds the EXACT pushed export (closes the release-tag TOCTOU).
    d = _load()
    captured = {}

    def runner(name, argv):
        captured[name] = argv
        return (0, "pushed ok")

    cmds = d.delivery_commands(tmp_path, "v1", "owner/ironclad")
    ok, _ = d.execute_delivery(True, "GO ok", cmds, runner, resolve_pushed_sha=lambda out: "abcdef123456")
    assert ok and "--target" in captured["release-create"] and "abcdef123456" in captured["release-create"]
    captured.clear()
    d.execute_delivery(True, "GO ok", d.delivery_commands(tmp_path, "v1", "o/r"), runner)   # no resolver
    assert "--target" not in captured["release-create"]                    # back-compat: no --target


def test_deliver_release_exception_is_halted_error_not_a_crash(tmp_path):
    # #348 S8 review fix: an unexpected exception in the leg yields a fail-closed halted-error outcome
    # (not a bare traceback), and the staged base is STILL disposed.
    d = _load()
    disposed = []

    def boom(b):
        raise RuntimeError("gate wiring blew up")

    ops = d.DeliverOps(stage_base=lambda: "BASE", deliver_gate=boom, authorize=lambda: (True, "x"),
                       execute=lambda a, w: (True, []), dispose=lambda b: disposed.append(b))
    out = d.deliver_release(ops)
    assert out.state == "DELIVER" and out.status == "halted-error"
    assert any("gate wiring blew up" in str(r) for r in out.reasons) and disposed == ["BASE"]


def test_deliver_release_halts_on_a_failed_execute(tmp_path):
    d = _load()
    ops, st = _deliver_ops(d, delivered=False)
    out = d.deliver_release(ops)
    assert out.state == "DELIVER" and out.status == "halted-execute"
    assert st["disposed"] == ["BASE"]


def test_deliver_release_stamps_the_delivery_marker_only_on_success(tmp_path):
    # #348 S9: a successful delivery stamps the guard-evidence marker (the merge-walk substrate) exactly
    # once; a red gate / parked / halted delivery stamps NOTHING.
    d = _load()
    stamped = []
    ok_ops = d.DeliverOps(stage_base=lambda: "B", deliver_gate=lambda b: _Gate(True),
                          authorize=lambda: (True, "ok"), execute=lambda a, w: (True, ["pushed"]),
                          dispose=lambda b: None, stamp=lambda: stamped.append("marker"))
    assert d.deliver_release(ok_ops).status == "delivered-pending" and stamped == ["marker"]
    stamped.clear()
    red_ops = d.DeliverOps(stage_base=lambda: "B", deliver_gate=lambda b: _Gate(False, ["red"]),
                           authorize=lambda: (True, "ok"), execute=lambda a, w: (True, []),
                           dispose=lambda b: None, stamp=lambda: stamped.append("marker"))
    d.deliver_release(red_ops)
    assert stamped == []                                            # never stamp a non-delivered outcome


def test_deliver_release_stamp_failure_after_ship_is_distinct_not_masked_as_halt(tmp_path):
    # #358 review (stamp-fail-open): execute() already shipped the push+release (irreversible). If the
    # evidence stamp then throws, the outcome must NOT be masked as "halted-error" (which reads as
    # nothing-shipped) — it must surface a DISTINCT DELIVERED/delivered-unrecorded so reconcile re-stamps.
    d = _load()
    def _boom():
        raise OSError("ledger append failed")
    ops = d.DeliverOps(stage_base=lambda: "B", deliver_gate=lambda b: _Gate(True),
                       authorize=lambda: (True, "ok"), execute=lambda a, w: (True, ["pushed", "released"]),
                       dispose=lambda b: None, stamp=_boom)
    out = d.deliver_release(ops)
    # #396 S14b: shipped-but-unstamped stays NON-terminal (DELIVER/delivered-unrecorded), distinct from a
    # halted-error (which reads as nothing-shipped); reconcile re-stamps + the completion gate later flips it.
    assert out.state == "DELIVER" and out.status == "delivered-unrecorded"   # NOT "halted-error", NOT terminal
    assert any("stamp failed" in r for r in out.reasons)
