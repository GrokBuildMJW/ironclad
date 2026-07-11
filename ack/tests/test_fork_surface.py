"""#1340 (S4 / epic #1344): durable project-scoped fork worker + /fork surface + decide→learn.

Covers P1 MPR artifact_slug port, P2 worker (context-per-item, switch-before-drain, safe-queue
rollback), P3 /fork list + supersession, P4 /fork decide + /approve split/block, P5 decide→learn,
and byte-identical default-off. Opaque fork ids only (no #N).
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from ack.ace.fork_envelope import ForkEnvelope, build_constraint_envelope, make_fork_id
from ack.ace.constraint_conflict import Conflict
from ack.ace import ReflectionWorker, Trajectory

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))
_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))
_SKILLS = Path(__file__).resolve().parents[2] / "skills"
if str(_SKILLS) not in sys.path:
    sys.path.insert(0, str(_SKILLS))

import gx10  # noqa: E402
import project_registry  # noqa: E402
from mpr.entry import _engine_deps, mpr_research_run  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


# Pre-S4 empty `/fork` fall-through (exact string when both flags are off).
_PRE_S4_FORK_EMPTY = (
    "No pending MPR fork proposals. When an architecture fork is declared and the gate "
    "`ace.fork_mpr.enabled` is on, its decision-matrix appears here as a recommendation."
)


def _hard_reset():
    for w in (gx10._ACE_WORKER, gx10._ACE_FORK_WORKER):
        try:
            w and w.stop()
        except Exception:
            pass
    gx10._ACE_WORKER = None
    gx10._ACE_FORK_WORKER = None
    gx10._ACE_STORE = None
    gx10._ACE_FORK_MPR = False
    gx10._ACE_FORK_INFLIGHT.clear()
    if hasattr(gx10, "_ACE_CONSTRAINT_ENVELOPE_INFLIGHT"):
        gx10._ACE_CONSTRAINT_ENVELOPE_INFLIGHT.clear()


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    _hard_reset()
    monkeypatch.setattr(project_registry, "ironclad_home", lambda: tmp_path)
    monkeypatch.setattr(gx10, "_active_mem_ns", lambda default="": "ns-test", raising=False)
    gx10._apply_config(gx10._code_defaults())
    monkeypatch.setattr(gx10, "DESIGN_GATE_ENABLED", False)
    monkeypatch.setattr(gx10, "CONSTRAINT_GATE_ENABLED", False)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", False)
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    yield
    _hard_reset()


def _setup_unit(monkeypatch, tmp_path, *, detect=False, fork_mpr=False):
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", detect)
    gx10._ACE_FORK_MPR = fork_mpr
    if fork_mpr:
        gx10._ACE_FORK_WORKER = ReflectionWorker(gx10._ace_fork_run_task)
    gx10.STORE = None
    return gx10.initiative_new("Unit-A", "software")


def _emit_envelope(slug: str, *, category="language", required="python", counter="rust",
                   crev="c1", drev="d1", mem_ns="ns-test") -> ForkEnvelope:
    conflict = Conflict(category=category, required=required, counter=counter)
    env = build_constraint_envelope(
        mem_ns=mem_ns, slug=slug, conflict=conflict,
        constraint_rev=crev, design_rev=drev,
    )
    gx10._persist_fork_envelope(env)
    return env


# --------------------------------------------------------------------------- #
# P1 — MPR artifact_slug port
# --------------------------------------------------------------------------- #


def test_artifact_slug_routes_runs_dir(tmp_path, monkeypatch):
    a = gx10.initiative_new("Alpha", "software")
    b = gx10.initiative_new("Beta", "software")
    # Active is B; port must still bind A.
    assert gx10.active_slug() == b.slug
    deps = _engine_deps(artifact_slug=a.slug)
    assert deps.runs_dir.replace("\\", "/").endswith(f"vault/{a.slug}/runs")
    assert b.slug not in deps.runs_dir.replace("\\", "/")
    assert deps.index_runs is False


def test_artifact_slug_none_is_byte_identical_to_active(tmp_path):
    v = gx10.initiative_new("Only", "software")
    d_none = _engine_deps()
    d_explicit_none = _engine_deps(artifact_slug=None)
    assert d_none.runs_dir == d_explicit_none.runs_dir
    assert d_none.runs_dir.replace("\\", "/").endswith(f"vault/{v.slug}/runs")
    assert d_none.index_runs is True


def test_artifact_slug_unknown_refuses(tmp_path):
    gx10.initiative_new("Live", "software")
    out = mpr_research_run("q?", artifact_slug="does-not-exist-xyz")
    assert out.startswith("ERROR")
    assert "unknown initiative" in out


def test_artifact_slug_empty_refuses(tmp_path):
    gx10.initiative_new("Live", "software")
    out = mpr_research_run("q?", artifact_slug="   ")
    assert out.startswith("ERROR")
    assert "empty" in out.lower()


# --------------------------------------------------------------------------- #
# P2 — durable worker
# --------------------------------------------------------------------------- #


def test_worker_fills_recommendation_under_envelope_slug(tmp_path, monkeypatch):
    a = _setup_unit(monkeypatch, tmp_path, fork_mpr=True)
    env = _emit_envelope(a.slug)
    calls = []

    def _tool(name, args):
        calls.append((name, dict(args)))
        return "## Decision matrix\nRecommendation: keep python\n..."

    monkeypatch.setattr(gx10, "run_tool", _tool)
    assert gx10._ace_submit_constraint_envelope(env) is True
    gx10._ACE_FORK_WORKER.process_pending()
    assert len(calls) == 1
    assert calls[0][0] == "mpr_research"
    assert calls[0][1].get("artifact_slug") == a.slug
    loaded = gx10._load_fork_envelopes(a.slug)
    assert len(loaded) == 1
    assert loaded[0].recommendation is not None
    assert "keep" in (loaded[0].recommendation.get("text") or "").lower()
    assert loaded[0].matrix and "Decision matrix" in loaded[0].matrix
    assert loaded[0].status == "pending"
    assert loaded[0].inflight is False


def test_switch_before_drain_uses_envelope_slug(tmp_path, monkeypatch):
    """Submit for A, switch active to B, drain → MPR artifact_slug is A not B."""
    a = gx10.initiative_new("Slug-A", "software")
    b = gx10.initiative_new("Slug-B", "software")
    gx10._ACE_FORK_MPR = True
    gx10._ACE_FORK_WORKER = ReflectionWorker(gx10._ace_fork_run_task)
    env = _emit_envelope(a.slug)
    # Switch active initiative to B BEFORE drain.
    gx10.initiative_use(b.slug)
    assert gx10.active_slug() == b.slug
    seen = []

    def _tool(name, args):
        seen.append(args.get("artifact_slug"))
        return "## Decision matrix\nRecommendation: keep\n"

    monkeypatch.setattr(gx10, "run_tool", _tool)
    gx10._ace_submit_constraint_envelope(env)
    gx10._ACE_FORK_WORKER.process_pending()
    assert seen == [a.slug]
    # Envelope under A is filled; B has no ledger pollution.
    filled = gx10._load_fork_envelopes(a.slug)
    assert filled and filled[0].recommendation is not None
    assert gx10._load_fork_envelopes(b.slug) == []


def test_drain_uses_submit_time_project_context(tmp_path, monkeypatch):
    """P2: copy_context at submit + ctx.run at drain — worker sees project A, not active B.

    Unlike ``test_switch_before_drain_uses_envelope_slug`` (which only checks the explicit
    ``artifact_slug`` arg and would also pass under a bind-at-drain impl), this asserts a
    **context-scoped** resolver (``_active_mem_ns`` / ``project_context.current``) resolves to
    the submit-time ProjectContext during drain.
    """
    import project_context as pc
    from project_context import ProjectContext

    a = gx10.initiative_new("Ctx-A", "software")
    gx10._ACE_FORK_MPR = True
    gx10._ACE_FORK_WORKER = ReflectionWorker(gx10._ace_fork_run_task)
    # Replace the autouse fixture's constant mem_ns stub with the real context-scoped
    # resolver so a bind-at-drain impl cannot hide behind the stub.
    def _real_active_mem_ns(default=""):
        cur = pc.current()
        if cur is not None and cur.mem_ns:
            return cur.mem_scope()
        return default

    monkeypatch.setattr(gx10, "_active_mem_ns", _real_active_mem_ns)
    # Same filesystem root so the ledger remains reachable; isolation under test is
    # the contextvars identity (mem_ns / project_id), not a second vault tree.
    ctx_a = ProjectContext("proj-a", str(tmp_path), "mem-ns-A")
    ctx_b = ProjectContext("proj-b", str(tmp_path), "mem-ns-B")
    env = _emit_envelope(a.slug, mem_ns="mem-ns-A")
    seen: dict = {}

    def _tool(name, args):
        # Resolvers that consult contextvars — must be A under ctx.run, not active B.
        seen["mem_ns"] = gx10._active_mem_ns(default="")
        cur = pc.current()
        seen["project_id"] = cur.project_id if cur else None
        seen["pc_mem_ns"] = cur.mem_ns if cur else None
        return "## Decision matrix\nRecommendation: keep\n"

    monkeypatch.setattr(gx10, "run_tool", _tool)
    # Capture context under A at SUBMIT.
    with pc.use(ctx_a):
        assert pc.current() is not None and pc.current().mem_ns == "mem-ns-A"
        assert gx10._active_mem_ns(default="") == "mem-ns-A"
        assert gx10._ace_submit_constraint_envelope(env) is True
    # Switch ACTIVE ProjectContext to B BEFORE drain (the point is contextvars, not slug args).
    with pc.use(ctx_b):
        assert pc.current().mem_ns == "mem-ns-B"
        assert gx10._active_mem_ns(default="") == "mem-ns-B"
        # Drain under B's active context — worker must still see A via ctx.run.
        gx10._ACE_FORK_WORKER.process_pending()
    assert seen.get("mem_ns") == "mem-ns-A", seen
    assert seen.get("pc_mem_ns") == "mem-ns-A", seen
    assert seen.get("project_id") == "proj-a", seen
    filled = gx10._load_fork_envelopes(a.slug)
    assert filled and filled[0].recommendation is not None


def test_worker_rollback_on_mpr_failure(tmp_path, monkeypatch):
    a = _setup_unit(monkeypatch, tmp_path, fork_mpr=True)
    env = _emit_envelope(a.slug)

    def _boom(name, args):
        raise RuntimeError("mpr blew up")

    monkeypatch.setattr(gx10, "run_tool", _boom)
    gx10._ace_submit_constraint_envelope(env)
    gx10._ACE_FORK_WORKER.process_pending()
    loaded = gx10._load_fork_envelopes(a.slug)[0]
    # Run lock is process-local; ledger must not carry a durable claim after failure.
    assert loaded.inflight is False
    assert env.fork_id not in gx10._ACE_CONSTRAINT_ENVELOPE_INFLIGHT
    assert loaded.recommendation is None
    assert loaded.status == "pending"
    # Retryable: re-submit succeeds after a working tool.
    monkeypatch.setattr(gx10, "run_tool", lambda n, a: "## Decision matrix\nRecommendation: keep\n")
    assert gx10._ace_submit_constraint_envelope(loaded) is True
    gx10._ACE_FORK_WORKER.process_pending()
    assert gx10._load_fork_envelopes(a.slug)[0].recommendation is not None


def test_stale_persisted_inflight_does_not_block_resubmit(tmp_path, monkeypatch):
    """#17: a durable inflight=True orphan (hard crash between claim and fill) must re-drain.

    The run lock is process-local only — persisted ``inflight`` must never gate resubmit.
    """
    a = _setup_unit(monkeypatch, tmp_path, fork_mpr=True)
    env = _emit_envelope(a.slug)
    # Simulate pre-fix durable orphan left by a hard crash (SIGKILL/OOM).
    env.inflight = True
    gx10._persist_fork_envelope(env)
    orphan = gx10._load_fork_envelopes(a.slug)[0]
    assert orphan.inflight is True
    assert orphan.recommendation is None
    assert orphan.status == "pending"
    # Fresh process-local set is empty → resubmit must succeed.
    assert env.fork_id not in gx10._ACE_CONSTRAINT_ENVELOPE_INFLIGHT
    monkeypatch.setattr(
        gx10, "run_tool",
        lambda n, a: "## Decision matrix\nRecommendation: keep python\n",
    )
    assert gx10._ace_submit_constraint_envelope(orphan) is True
    gx10._ACE_FORK_WORKER.process_pending()
    filled = gx10._load_fork_envelopes(a.slug)[0]
    assert filled.recommendation is not None
    assert filled.inflight is False
    assert env.fork_id not in gx10._ACE_CONSTRAINT_ENVELOPE_INFLIGHT


def test_worker_gate_off_is_noop(tmp_path, monkeypatch):
    a = _setup_unit(monkeypatch, tmp_path, fork_mpr=False)
    env = _emit_envelope(a.slug)
    calls = []
    monkeypatch.setattr(gx10, "run_tool", lambda n, a: calls.append(1) or "x")
    assert gx10._ace_submit_constraint_envelope(env) is False
    assert gx10._ace_scan_constraint_envelopes(a.slug) == 0
    assert calls == []
    assert gx10._load_fork_envelopes(a.slug)[0].recommendation is None


# --------------------------------------------------------------------------- #
# P3 — /fork list + supersession
# --------------------------------------------------------------------------- #


def test_fork_list_shows_pending_opaque_ids(tmp_path, monkeypatch):
    a = _setup_unit(monkeypatch, tmp_path)
    env = _emit_envelope(a.slug)
    out = gx10._fork_command("")
    assert env.fork_id in out
    assert "question:" in out
    assert "keep" in out and "counter" in out
    assert f"#{env.fork_id}" not in out  # opaque — no # prefix on the id
    assert "#N" not in out
    out2 = gx10._fork_command("list")
    assert env.fork_id in out2


def test_fork_list_supersedes_older_same_category(tmp_path, monkeypatch):
    a = _setup_unit(monkeypatch, tmp_path)
    old = _emit_envelope(a.slug, crev="c1", drev="d1")
    # Newer same category (different revs → different fork_id).
    import time
    time.sleep(0.02)  # ensure mtime ordering
    new = _emit_envelope(a.slug, crev="c1", drev="d2")
    assert old.fork_id != new.fork_id
    out = gx10._fork_command("list")
    assert new.fork_id in out
    assert old.fork_id not in out  # superseded, not listed
    loaded = {e.fork_id: e for e in gx10._load_fork_envelopes(a.slug)}
    assert loaded[old.fork_id].status == "superseded"
    assert loaded[new.fork_id].status == "pending"


def test_fork_list_falls_through_when_no_envelopes(tmp_path, monkeypatch):
    """No envelope ledger → M5 unit-proposal path (byte-identical empty message)."""
    _setup_unit(monkeypatch, tmp_path)
    out = gx10._fork_command("")
    assert out == _PRE_S4_FORK_EMPTY
    assert "fork_id:" not in out


# --------------------------------------------------------------------------- #
# P4 — /fork decide + /approve split
# --------------------------------------------------------------------------- #


def test_fork_decide_keep(tmp_path, monkeypatch):
    a = _setup_unit(monkeypatch, tmp_path)
    gx10.record_constraints("Scope", "Python only", language="python")
    env = _emit_envelope(a.slug)
    typed_before = gx10._constraint_typed(a.slug)
    out = gx10._fork_command(f"decide {env.fork_id} --choice keep")
    assert out.startswith("OK")
    assert "UNCHANGED" in out or "unchanged" in out.lower() or "must comply" in out.lower()
    loaded = gx10._load_fork_envelopes(a.slug)[0]
    assert loaded.status == "resolved"
    assert loaded.resolution["choice_id"] == "keep"
    assert gx10._constraint_typed(a.slug) == typed_before  # constraint floor unchanged


def test_fork_decide_counter(tmp_path, monkeypatch):
    a = _setup_unit(monkeypatch, tmp_path)
    gx10.record_constraints("Scope", "Python only", language="python")
    env = _emit_envelope(a.slug, required="python", counter="rust")
    out = gx10._fork_command(f"decide {env.fork_id} --choice counter")
    assert out.startswith("OK")
    assert "operator-override" in out
    loaded = gx10._load_fork_envelopes(a.slug)[0]
    assert loaded.status == "resolved"
    assert loaded.resolution["choice_id"] == "counter"
    typed = gx10._constraint_typed(a.slug)
    assert typed.get("language") == "rust"


def test_fork_decide_counter_stamps_only_counter_category(tmp_path, monkeypatch):
    a = _setup_unit(monkeypatch, tmp_path, detect=True)
    gx10.record_constraints("Scope", "Python only, no network", language="python", network="none",
                            source="suggested")
    gx10._approve_constraint("language")
    env = _emit_envelope(a.slug, required="python", counter="rust")

    out = gx10._fork_command(f"decide {env.fork_id} --choice counter")

    assert out.startswith("OK")
    text = (gx10.vault_root() / a.slug / "decisions" / "constraints.md").read_text(encoding="utf-8")
    assert "source_language: operator-override\n" in text
    typed = gx10._constraint_typed(a.slug)
    unresolved = gx10._constraint_typed_unresolved(a.slug)
    assert typed.get("language") == "rust"
    assert "network" not in typed
    assert unresolved.get("network") is False
    assert "language" not in unresolved


def test_fork_decide_idempotent(tmp_path, monkeypatch):
    a = _setup_unit(monkeypatch, tmp_path)
    gx10.record_constraints("Scope", "Python only", language="python")
    env = _emit_envelope(a.slug)
    assert gx10._fork_command(f"decide {env.fork_id} --choice keep").startswith("OK")
    out2 = gx10._fork_command(f"decide {env.fork_id} --choice keep")
    assert "idempotent" in out2.lower() or "already resolved" in out2.lower()
    assert out2.startswith("OK")


def test_fork_decide_unknown_and_resolved_mismatch(tmp_path, monkeypatch):
    a = _setup_unit(monkeypatch, tmp_path)
    gx10.record_constraints("Scope", "Python only", language="python")
    env = _emit_envelope(a.slug)
    assert "unknown" in gx10._fork_command("decide deadbeefdeadbeef --choice keep").lower()
    gx10._fork_command(f"decide {env.fork_id} --choice keep")
    out = gx10._fork_command(f"decide {env.fork_id} --choice counter")
    assert out.startswith("ERROR")
    assert "already resolved" in out.lower()


def test_approve_design_blocked_while_pending(tmp_path, monkeypatch):
    a = _setup_unit(monkeypatch, tmp_path)
    gx10.record_constraints("Scope", "Python only", language="python")
    gx10.record_design("Approach", "use Rust", language="rust")
    env = _emit_envelope(a.slug)
    out = gx10._approve_command("design")
    assert out.startswith("ERROR")
    assert env.fork_id in out
    assert "pending constraint fork" in out.lower()
    # After decide keep, design still not auto-edited — but block lifts.
    gx10._fork_command(f"decide {env.fork_id} --choice keep")
    out2 = gx10._approve_command("design")
    # Design may still not be approved if record_design didn't run with detect —
    # we recorded design separately; after keep the pending is gone so approve can proceed.
    assert "pending constraint fork" not in out2.lower()


def test_approve_design_resolves_stale_fork_after_realignment(tmp_path, monkeypatch):
    a = _setup_unit(monkeypatch, tmp_path, detect=True)
    gx10.record_constraints("Scope", "Python only", language="python")
    gx10.record_design("Approach", "use Rust", language="rust")
    pending = gx10._pending_constraint_forks(a.slug)
    assert pending
    fid = pending[0].fork_id

    gx10.record_design("Approach", "use Python", language="python")
    out = gx10._approve_command("design")

    assert out.startswith("OK: approved the design")
    resolved = gx10._find_fork_envelope(fid, a.slug)
    assert resolved.status == "resolved"
    assert resolved.resolution["choice_id"] == "realigned"
    assert resolved.resolution["value"] == "python"
    assert not any(env.fork_id == fid for env in gx10._pending_constraint_forks(a.slug))


def test_approve_design_keeps_pending_fork_when_design_still_conflicts(tmp_path, monkeypatch):
    a = _setup_unit(monkeypatch, tmp_path, detect=True)
    gx10.record_constraints("Scope", "Python only", language="python")
    gx10.record_design("Approach", "use Rust", language="rust")
    fid = gx10._pending_constraint_forks(a.slug)[0].fork_id

    out = gx10._approve_command("design")

    assert out.startswith("ERROR: pending constraint fork")
    assert fid in out
    still_pending = gx10._find_fork_envelope(fid, a.slug)
    assert still_pending.status == "pending"


def test_approve_design_detect_off_does_not_auto_resolve_pending_fork(tmp_path, monkeypatch):
    a = _setup_unit(monkeypatch, tmp_path, detect=False)
    gx10.record_constraints("Scope", "Python only", language="python")
    gx10.record_design("Approach", "use Python", language="python")
    env = _emit_envelope(a.slug)

    out = gx10._approve_command("design")

    assert out.startswith("ERROR: pending constraint fork")
    assert env.fork_id in out
    assert gx10._find_fork_envelope(env.fork_id, a.slug).status == "pending"


def test_approve_design_fail_closed_on_ledger_error(tmp_path, monkeypatch):
    """N2: CONSTRAINT_CONFLICT_DETECT on + ledger read raises → refuse (fail-closed)."""
    a = _setup_unit(monkeypatch, tmp_path, detect=True)
    gx10.record_constraints("Scope", "Python only", language="python")
    gx10.record_design("Approach", "ok")
    monkeypatch.setattr(
        gx10, "_visible_pending_fork_envelopes",
        lambda slug, fail_closed=False: (_ for _ in ()).throw(OSError("ledger boom")),
    )
    out = gx10._approve_command("design")
    assert out.startswith("ERROR")
    assert "fail-closed" in out.lower() or "ledger" in out.lower()
    assert "nothing changed" in out.lower()
    # Design must not have been approved.
    text = (gx10.vault_root() / a.slug / "decisions" / "design.md").read_text(encoding="utf-8")
    assert "approved: true" not in text.lower().replace(" ", "")


def test_approve_design_ledger_error_flag_off_no_block(tmp_path, monkeypatch):
    """N2: flag off + ledger error → no approve block (byte-identical / fail-soft)."""
    a = _setup_unit(monkeypatch, tmp_path, detect=False)
    gx10.record_constraints("Scope", "Python only", language="python")
    gx10.record_design("Approach", "ok")
    monkeypatch.setattr(
        gx10, "_visible_pending_fork_envelopes",
        lambda slug, fail_closed=False: (_ for _ in ()).throw(OSError("ledger boom")),
    )
    out = gx10._approve_command("design")
    assert "pending constraint fork" not in out.lower()
    assert "ledger" not in out.lower()
    assert out.startswith("OK") or "already approved" in out.lower()


def test_approve_split_bare_is_design(tmp_path, monkeypatch):
    a = _setup_unit(monkeypatch, tmp_path)
    gx10.record_design("Approach", "ok")
    out = gx10._approve_command("")
    assert "approved the design" in out.lower() or "already approved" in out.lower()


def test_approve_constraint_promotes_suggested(tmp_path, monkeypatch):
    a = _setup_unit(monkeypatch, tmp_path)
    gx10.record_constraints("Scope", "Python only", language="python", source="suggested")
    assert gx10._constraint_typed(a.slug) == {}  # suggested excluded
    out = gx10._approve_command("constraint all")
    assert out.startswith("OK")
    assert gx10._constraint_typed(a.slug).get("language") == "python"


# --------------------------------------------------------------------------- #
# P5 — decide→learn
# --------------------------------------------------------------------------- #


def test_decide_learn_submits_trajectory(tmp_path, monkeypatch):
    a = _setup_unit(monkeypatch, tmp_path, fork_mpr=True)
    gx10.record_constraints("Scope", "Python only", language="python")
    env = _emit_envelope(a.slug, mem_ns="ns-learn")
    submitted = []

    class _W:
        def submit(self, item):
            submitted.append(item)
            return True

        def stop(self):
            pass

    gx10._ACE_WORKER = _W()
    gx10._ACE_FORK_MPR = True
    gx10._fork_command(f"decide {env.fork_id} --choice keep")
    assert len(submitted) == 1
    assert submitted[0]["scope"] == "ns-learn"
    traj = submitted[0]["trajectory"]
    assert isinstance(traj, Trajectory)
    assert "constraint fork" in traj.query
    assert "keep" in traj.outcome


def test_decide_learn_gate_off_is_noop(tmp_path, monkeypatch):
    a = _setup_unit(monkeypatch, tmp_path, fork_mpr=False)
    gx10.record_constraints("Scope", "Python only", language="python")
    env = _emit_envelope(a.slug)
    submitted = []

    class _W:
        def submit(self, item):
            submitted.append(item)
            return True

        def stop(self):
            pass

    gx10._ACE_WORKER = _W()
    gx10._ACE_FORK_MPR = False
    gx10._fork_command(f"decide {env.fork_id} --choice keep")
    assert submitted == []


# --------------------------------------------------------------------------- #
# Byte-identical default-off + command_spec
# --------------------------------------------------------------------------- #


def test_byte_identical_both_flags_off(tmp_path, monkeypatch):
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", False)
    gx10._ACE_FORK_MPR = False
    gx10._ACE_FORK_WORKER = None
    a = gx10.initiative_new("Plain", "software")
    gx10.record_constraints("Scope", "Python only", language="python")
    gx10.record_design("Approach", "use Rust", language="rust")
    # No ledger written.
    assert gx10._load_fork_envelopes(a.slug) == []
    # /fork empty path — FULL exact pre-S4 string (not a substring).
    out = gx10._fork_command("")
    assert out == _PRE_S4_FORK_EMPTY
    assert "Typed HARD conflicts" not in out
    # /approve design unblocked (no pending envelope).
    out2 = gx10._approve_command("")
    assert "pending constraint fork" not in out2.lower()
    assert out2.startswith("OK") or "already approved" in out2.lower()


def test_command_spec_fork_and_approve(tmp_path):
    import command_spec as cs
    fork = cs.by_verb("fork")
    assert fork is not None
    assert "list" in (fork.subcommands or ())
    assert "decide" in (fork.subcommands or ())
    assert fork.tier == cs.MUTATING
    ap = cs.by_verb("approve")
    assert ap is not None
    assert "design" in (ap.subcommands or ())
    assert "constraint" in (ap.subcommands or ())


def test_fork_envelope_inflight_round_trip():
    """Legacy inflight field still round-trips, but is not an authoritative run lock."""
    env = ForkEnvelope(fork_id="abc", slug="s", inflight=True, status="pending")
    d = env.to_dict()
    assert d["inflight"] is True
    restored = ForkEnvelope.from_dict(d)
    assert restored.inflight is True
    # Legacy dict without inflight key → False
    assert ForkEnvelope.from_dict({"fork_id": "x"}).inflight is False
