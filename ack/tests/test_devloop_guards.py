"""Machine-gated dev-loop structured guards (epic #262, S2 / ADR 0002), offline.

Lives in `scripts/devloop/` (private) -> skips in an installed/clean-room tree. Pins the guard
framework: exit-code wrapping is **fail-closed** (a missing binary is RED, not a silent pass),
`compose` aggregates a profile correctly, `english_only` flags German characters, and
`gate_profile_commands` composes the target-agnostic guards everywhere AND the CORE_ONLY shell guards
for a core/ target (epic #312 S1 — the phase-2 composer). Each behaviour has its positive AND its
negative case (ADR-0007 discipline).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_GUARDS = _REPO / "scripts" / "devloop" / "guards.py"

pytestmark = pytest.mark.skipif(
    not _GUARDS.is_file(),
    reason="private dev-loop guards (scripts/devloop/guards.py) absent — installed/clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_devloop_guards", _GUARDS)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_shell_guard_pass_and_fail(tmp_path):
    g = _load()
    assert g.shell_guard("ok", [sys.executable, "-c", "import sys; sys.exit(0)"], tmp_path)
    red = g.shell_guard("bad", [sys.executable, "-c", "import sys; sys.exit(1)"], tmp_path)
    assert not red and red.reasons


def test_shell_guard_is_fail_closed_on_missing_binary(tmp_path):
    g = _load()
    r = g.shell_guard("nope", ["definitely-not-a-real-binary-xyz123"], tmp_path)
    assert not r.passed                                  # an unrunnable gate is RED, never a pass
    assert any("fail-closed" in x for x in r.reasons)


def test_shell_guard_decodes_utf8_guard_output(tmp_path):
    # #342: the gate decodes child output as UTF-8 (not the Windows cp1252 locale), so a guard that emits
    # non-ASCII (glyphs like the arrow / check, accented text) is captured into the reason, never crashing
    # the reader thread. The child writes raw UTF-8 BYTES so its own stdout encoding is irrelevant; the
    # literals are \u-escaped so THIS test source stays pure ASCII.
    g = _load()
    prog = ("import sys; sys.stderr.buffer.write("
            "'caf\\u00e9 \\u2192 \\u2713 boundary fail\\n'.encode('utf-8')); sys.exit(1)")
    r = g.shell_guard("boundary", [sys.executable, "-c", prog], tmp_path)
    assert not r.passed and r.reasons
    assert "boundary fail" in r.reasons[0]                  # ASCII part captured (reader did not crash)
    assert "✓" in r.reasons[0] and "café" in r.reasons[0]   # non-ASCII decoded intact, not garbled


def test_compose_all_green_vs_any_red():
    g = _load()
    ok = [g.GuardResult("a", True), g.GuardResult("b", True)]
    assert g.compose("profile", ok).passed
    mixed = [g.GuardResult("a", True), g.GuardResult("b", False, ["b broke"])]
    out = g.compose("profile", mixed)
    assert not out.passed and "b broke" in out.reasons


def test_english_only_flags_german_and_passes_clean(tmp_path):
    g = _load()
    en = tmp_path / "ok.py"; en.write_text("def run(text):\n    return text.lower()\n", encoding="utf-8")
    de = tmp_path / "bad.py"; de.write_text("# zaehle die Fuesse\nGROESSE = 1  # Fuelldaten: Groesse\n".replace("ue", "ü").replace("oe", "ö"), encoding="utf-8")
    assert g.english_only("en", [en]).passed
    red = g.english_only("de", [de])
    assert not red.passed and "english-only" in red.reasons[0]


def test_gate_profile_composes_core_shell_guards():
    g = _load()
    plugin = {"boundary_cmd": "python scripts/check_plugin_boundary.py .",
              "gate_profile": ["boundary", "pytest"]}
    core = {"boundary_cmd": "python scripts/ci/check_core_boundary.py",
            "gate_profile": ["boundary", "pytest", "doc-reality-audit", "test-counts", "node-boundary",
                             "english-only", "secret-scan", "deploy-consistency"]}
    pc = g.gate_profile_commands(plugin, ".")
    cc = g.gate_profile_commands(core, "/base")
    # plugin loop is unchanged (only target-agnostic guards in its profile)
    assert set(pc) == {"boundary", "pytest"}
    assert pc["boundary"][0] == sys.executable and "check_plugin_boundary.py" in pc["boundary"][1]
    # core/ target now composes the CORE_ONLY SHELL guards too (epic #312 S1) — but NOT english-only
    # (that is the english_only() scanner, composed separately by the caller)
    assert set(cc) == {"boundary", "pytest", "doc-reality-audit", "test-counts", "node-boundary",
                       "secret-scan", "deploy-consistency"}
    # the guard scripts are the base-root's OWN copies (S2 integrity-pinned source), real argv
    assert cc["doc-reality-audit"][0] == sys.executable
    assert cc["doc-reality-audit"][1].replace("\\", "/").endswith("/base/scripts/ci/doc_reality_audit.py")
    assert cc["doc-reality-audit"][2].replace("\\", "/").endswith("/base/.export/ironclad")   # staged export, default path
    assert cc["test-counts"][1].replace("\\", "/").endswith("/base/scripts/ci/gen_test_counts.py") and cc["test-counts"][2] == "--check"
    assert cc["node-boundary"][0] == "node" and cc["node-boundary"][1].replace("\\", "/").endswith("/base/scripts/ci/check_node_boundary.mjs")
    # secret-scan = export_core.py --require-scanner (stages + gitleaks-scans); deploy-consistency = --check
    assert cc["secret-scan"][1].replace("\\", "/").endswith("/base/scripts/ci/export_core.py") and cc["secret-scan"][2] == "--require-scanner"
    assert cc["deploy-consistency"][1].replace("\\", "/").endswith("/base/scripts/ci/check_deploy_consistency.py") and cc["deploy-consistency"][2] == "--check"


def test_gate_profile_export_dir_override():
    # the doc-reality-audit guard audits the staged export at the path the gate passes (S1/S2)
    g = _load()
    core = {"boundary_cmd": "python scripts/ci/check_core_boundary.py",
            "gate_profile": ["doc-reality-audit"]}
    cc = g.gate_profile_commands(core, "/base", export_dir="/stage/ironclad")
    assert cc["doc-reality-audit"][2].replace("\\", "/") == "/stage/ironclad"


# ── gate <-> CI parity (epic #312 S1): the gate covers every PR-triggered ci.yml job ──
def test_ci_parity_real_ci_yml_fully_covered():
    g = _load()
    ci = _REPO / ".github" / "workflows" / "ci.yml"
    if not ci.is_file():
        pytest.skip("ci.yml absent")
    sp = importlib.util.spec_from_file_location("_devloop_spec_parity", _REPO / "scripts" / "devloop" / "spec.py")
    spec_mod = importlib.util.module_from_spec(sp); sys.modules[sp.name] = spec_mod; sp.loader.exec_module(spec_mod)
    gate_profile = spec_mod.TARGETS["core-monorepo"]["gate_profile"]
    jobs = g.parse_ci_jobs(ci.read_text(encoding="utf-8"))
    assert jobs, "parse_ci_jobs found no jobs — parser drift"
    assert g.ci_parity_violations(jobs, gate_profile) == []          # the REAL gate covers the REAL CI jobs


def test_ci_parity_flags_drift_and_missing_guard_but_accepts_defer():
    g = _load()
    # a custom job_map so the assertions are independent of the real CI_JOB_MAP's evolution (#387):
    # a string guard, a separate guard job, and a LIST-mapped folded job with a DEFER element.
    jm = {
        "checks": ["boundary", ("DEFER", "generated, not per-unit code")],
        "secret-scan": "secret-scan",
    }
    # an unmapped CI job => gate<->CI drift
    v1 = g.ci_parity_violations({"checks", "brand-new-job"}, ["boundary", "pytest"], job_map=jm)
    assert any("brand-new-job" in x and "drift" in x for x in v1) and len(v1) == 1
    # a mapped job whose guard is absent from the gate_profile => the gate misses a CI check
    v2 = g.ci_parity_violations({"secret-scan"}, ["boundary", "pytest"], job_map=jm)
    assert any("secret-scan" in x and "NOT in the gate_profile" in x for x in v2)
    # a LIST-mapped folded job: the DEFER element is accepted and the guard element is in the profile
    assert g.ci_parity_violations({"checks"}, ["boundary"], job_map=jm) == []
    # a LIST-mapped folded job whose guard element is absent from the profile => flagged per-element
    v3 = g.ci_parity_violations({"checks"}, ["pytest"], job_map=jm)
    assert any("checks" in x and "boundary" in x and "NOT in the gate_profile" in x for x in v3)


def test_ci_yml_is_actions_economical_pr_only_with_concurrency():
    """#387: ci.yml must NOT re-run on push:main (the PR verified the squash tree; main is covered by the
    next PR + devloop-on-merge.yml) and MUST cancel a superseded in-flight run — both are the Actions-
    minute saving. Negative guard: a reintroduced push:main trigger (the redundant re-run) fails here."""
    g = _load()
    ci = _REPO / ".github" / "workflows" / "ci.yml"
    if not ci.is_file():
        pytest.skip("ci.yml absent")
    import re
    text = ci.read_text(encoding="utf-8")
    # trigger keys are 2-space-indented under `on:` — checking the indented key (not the header prose,
    # which legitimately says "NOT on push:main") keeps the assertion immune to comments.
    assert re.search(r"(?m)^  pull_request:", text)
    assert not re.search(r"(?m)^  push:", text), "ci.yml re-runs on push:main — the redundant Actions spend #387 forbids"
    assert "cancel-in-progress: true" in text, "ci.yml must cancel superseded runs (#387)"
    # the fast guards are folded into the single `checks` job, not eight separate billed jobs
    jobs = g.parse_ci_jobs(text)
    assert jobs == {"checks", "tests", "secret-scan"}, f"unexpected ci.yml job set (folding drift #387): {jobs}"


# ── #348 S4 DELIVER composer (deliver_profile_commands, parallel to gate_profile_commands) ──
def _load_spec():
    import importlib.util
    s = importlib.util.spec_from_file_location("_devloop_spec_g", _REPO / "scripts" / "devloop" / "spec.py")
    m = importlib.util.module_from_spec(s); sys.modules[s.name] = m; s.loader.exec_module(m)
    return m


def test_delivery_gates_match_spec_no_drift():
    # guards.DELIVERY_GATES (the composer's set) must equal spec._PHASE2B_DELIVERY (the validate() set) —
    # else the GATE could leak a delivery gate the DELIVER composer doesn't know, or vice versa.
    g = _load()
    assert set(g.DELIVERY_GATES) == set(_load_spec()._PHASE2B_DELIVERY)


def test_deliver_profile_commands_maps_delivery_gates_only(tmp_path):
    g = _load()
    target = {"dod_profile": ["tests-unit", "docs-public-grade", "clean-room", "release-preflight", "export-sync"]}
    cmds = g.deliver_profile_commands(target, tmp_path, tag="v9.9.9")
    # only the delivery gates are emitted (the per-unit DoD entries are the GATE's job)
    assert set(cmds) == {"clean-room", "release-preflight", "export-sync"}
    # release-preflight uses the canonical Python path with the threaded tag
    rp = cmds["release-preflight"]
    assert rp[-3:] == ["--preflight", "--tag", "v9.9.9"] and rp[1].endswith("release_preflight.py")
    # export-sync (S5) is the real PRE-push staged-tree leg (export_sync_check.py --staged <export>)
    es = cmds["export-sync"]
    assert es[1].endswith("export_sync_check.py") and "--staged" in es and es[-1].endswith("ironclad")
    # clean-room (S6) is the real PRE-publish proof runner (clean_room.py --staged <export>) — no sentinel left
    cr = cmds["clean-room"]
    assert cr[1].endswith("clean_room.py") and "--staged" in cr and cr[-1].endswith("ironclad")


def test_deliver_profile_commands_routes_index_url_from_release_index(tmp_path):
    # #397 S14c: the release-preflight 'already published' check hits the SAME index the cut publishes to.
    g = _load()
    base = {"dod_profile": ["release-preflight"]}
    testpypi = g.deliver_profile_commands({**base, "release_index": "testpypi"}, tmp_path, tag="v1")["release-preflight"]
    assert "--index-url" in testpypi and "https://test.pypi.org/pypi" in testpypi
    prod = g.deliver_profile_commands({**base, "release_index": "pypi"}, tmp_path, tag="v1")["release-preflight"]
    assert "--index-url" in prod and "https://pypi.org/pypi" in prod
    none = g.deliver_profile_commands(base, tmp_path, tag="v1")["release-preflight"]   # no release_index
    assert "--index-url" not in none                                                   # defaults to production


def test_deliver_profile_commands_empty_without_delivery_gates(tmp_path):
    g = _load()
    assert g.deliver_profile_commands({"dod_profile": ["tests-unit", "docs-public-grade"]}, tmp_path) == {}
