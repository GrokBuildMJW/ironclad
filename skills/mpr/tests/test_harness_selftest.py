"""Deterministic checks for the A/B-Harness pure/I-O functions (Spec 08 §3.2/§3.3/§3.4).

The harness is stdlib-only (ctx_harness style) and lives in ``../eval/`` which is NOT a package, so it
is loaded standalone via importlib (same mechanism the ironclad loader uses). Only the model-/net-free
functions are exercised here — the live ``run_arm``/``main`` path is operator-run (Gate §7 stufe 4), not
in this gate. Mirrors the perf format verified at gx10.py:2206-2213.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_HARNESS = Path(__file__).resolve().parents[1] / "eval" / "harness.py"


def _load():
    spec = importlib.util.spec_from_file_location("mpr_harness_probe", _HARNESS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


H = _load()


# ── §3.4 extract_perf (pure) ─────────────────────────────────────────────────────────────────────
def test_extract_perf_parses_the_engine_perf_line():
    # exact engine shape: TTFT {s}s · {ct} tok/{gt}s = {rate} tok/s · prompt {n}  (gx10.py:2206-2213)
    p = H.extract_perf("[perf] TTFT 0.5s · 120 tok/2.0s = 60 tok/s · prompt 2175")
    assert p == {"prompt_tokens": 2175, "ttft_s": 0.5, "completion_tokens": 120}


def test_extract_perf_completion_is_count_not_rate():
    # completion must be {ct} (before tok/{gt}s), not the trailing rate `60 tok/s`.
    assert H.extract_perf("300 tok/5.0s = 60 tok/s")["completion_tokens"] == 300


def test_extract_perf_last_prompt_wins_and_missing_is_none():
    assert H.extract_perf("prompt 10 ... prompt 20")["prompt_tokens"] == 20
    assert H.extract_perf("no perf line") == {"prompt_tokens": None, "ttft_s": None, "completion_tokens": None}
    assert H.extract_perf(None)["prompt_tokens"] is None


# ── §3.4 estimate_cost (pure) ────────────────────────────────────────────────────────────────────
_PRICING = {"claude-sonnet": {"cost_per_1k_in_usd": 0.003, "cost_per_1k_out_usd": 0.015},
            "spark-vllm": {"cost_per_1k_in_usd": 0.0, "cost_per_1k_out_usd": 0.0}}


def test_estimate_cost_sums_in_and_out_per_provider():
    persps = [{"provider": "claude-sonnet", "tokens": {"prompt": 1000, "completion": 1000}},
              {"provider": "spark-vllm", "tokens": {"prompt": 2000, "completion": 500}}]
    assert H.estimate_cost(persps, _PRICING) == 0.018      # 0.003 + 0.015 ; local adds 0


def test_estimate_cost_unknown_provider_and_missing_tokens_are_zero():
    assert H.estimate_cost([{"provider": "ghost", "tokens": {"prompt": 9999}}], _PRICING) == 0.0
    assert H.estimate_cost([{"provider": "claude-sonnet"}], _PRICING) == 0.0   # no tokens → 0
    assert H.estimate_cost([], _PRICING) == 0.0


# ── §3 diff_arms + assert_off_byte_identical (pure) ──────────────────────────────────────────────
def test_diff_arms_pairs_by_id_with_deltas():
    a = [{"id": "q1", "answer": "X", "ok": True, "cost_usd": 0.02, "latency_s": 3.0,
          "perf": {"prompt_tokens": 100, "completion_tokens": 50}}]
    b = [{"id": "q1", "answer": "Y", "ok": True, "cost_usd": 0.0, "latency_s": 1.0,
          "perf": {"prompt_tokens": 80, "completion_tokens": 40}}]
    d = H.diff_arms(a, b)["q1"]
    assert d["a_answer"] == "X" and d["b_answer"] == "Y"
    assert d["cost_delta_usd"] == 0.02 and d["latency_delta_s"] == 2.0


def _ok(i, a):
    return {"id": i, "answer": a, "ok": True}


def test_assert_off_byte_identical_passes_and_raises():
    H.assert_off_byte_identical([_ok("q1", "same")], [_ok("q1", "same")])
    with pytest.raises(AssertionError):                       # answer drift
        H.assert_off_byte_identical([_ok("q1", "a")], [_ok("q1", "b")])
    with pytest.raises(AssertionError):                       # id-set mismatch
        H.assert_off_byte_identical([_ok("q1", "x")], [_ok("q2", "x")])


def test_assert_off_byte_identical_is_fail_closed():
    # MED-A: the strongest regression gate must NOT green vacuously or on mutual failure / dup ids.
    with pytest.raises(AssertionError):                       # empty arms → nothing compared
        H.assert_off_byte_identical([], [])
    with pytest.raises(AssertionError):                       # mutual failure ≠ parity
        H.assert_off_byte_identical([{"id": "q1", "answer": None, "ok": False}],
                                    [{"id": "q1", "answer": None, "ok": False}])
    with pytest.raises(AssertionError):                       # one side failed
        H.assert_off_byte_identical([_ok("q1", "x")], [{"id": "q1", "answer": None, "ok": False}])
    with pytest.raises(AssertionError):                       # duplicate ids → ambiguous
        H.assert_off_byte_identical([_ok("q1", "a"), _ok("q1", "b")], [_ok("q1", "a")])


def test_manifest_perspectives_inline_and_file(tmp_path):
    # MED-B: cost wiring source — inline response wins; else load runs_dir/<run_id>/manifest.json; else None.
    assert H._manifest_perspectives({"perspectives": [{"provider": "x"}]}, None, None) == [{"provider": "x"}]
    rid = "mpr-run-1"
    (tmp_path / rid).mkdir()
    (tmp_path / rid / "manifest.json").write_text(
        json.dumps({"perspectives": [{"provider": "claude-sonnet", "tokens": {"prompt": 1000, "completion": 1000}}]}),
        encoding="utf-8")
    persps = H._manifest_perspectives({}, rid, str(tmp_path))
    assert persps and H.estimate_cost(persps, _PRICING) == 0.018      # manifest → cost wired
    assert H._manifest_perspectives({}, "missing", str(tmp_path)) is None    # fail-soft
    assert H._manifest_perspectives({}, None, None) is None


# ── §3.2 load_set/load_refs/write_report (I/O, tmp_path) ──────────────────────────────────────────
def test_load_set_parses_jsonl_skipping_blanks(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text('{"id":"q1","query":"a","domain":"d"}\n\n{"id":"q2","query":"b"}\n', encoding="utf-8")
    got = H.load_set(str(f))
    assert [r["id"] for r in got] == ["q1", "q2"]


def test_load_refs_roundtrip(tmp_path):
    f = tmp_path / "r.json"
    f.write_text('{"q1": {"axes": ["x", "y"]}}', encoding="utf-8")
    assert H.load_refs(str(f))["q1"]["axes"] == ["x", "y"]


def test_write_report_emits_json_and_md(tmp_path):
    diff = {"q1": {"query": "a", "domain": "arch", "a_ok": True, "b_ok": True,
                   "a_prompt_tokens": 100, "b_prompt_tokens": 80, "a_completion_tokens": 50,
                   "b_completion_tokens": 40, "cost_delta_usd": 0.02, "latency_delta_s": 2.0}}
    jp = H.write_report(diff, str(tmp_path / "ab"))
    assert json.loads(Path(jp).read_text(encoding="utf-8")) == diff       # git-diffbar, lossless
    md = (tmp_path / "ab" / "report.md").read_text(encoding="utf-8")
    assert "MPR A/B-Report" in md and "q1" in md


# ── §7 stufe 3: the CLI selftest runs clean (no net) ─────────────────────────────────────────────
def test_cli_selftest_runs_clean(capsys):
    H._selftest()
    assert "harness selftest: OK" in capsys.readouterr().out


def test_harness_is_stdlib_only():
    """The harness must not import the mpr package or any third-party dep (ctx_harness doctrine §3)."""
    import ast
    tree = ast.parse(_HARNESS.read_text(encoding="utf-8"))
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    allowed = {"argparse", "json", "re", "time", "urllib", "pathlib", "typing", "__future__"}
    assert mods <= allowed, f"harness pulled non-stdlib/forbidden imports: {sorted(mods - allowed)}"
    # and no dynamic import laundering (importlib.import_module / __import__) sneaking a dep past the scan
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    dyn = [c for c in calls if (isinstance(c.func, ast.Name) and c.func.id == "__import__")
           or (isinstance(c.func, ast.Attribute) and c.func.attr == "import_module")]
    assert not dyn, "harness uses dynamic import — defeats the stdlib-only guard"
