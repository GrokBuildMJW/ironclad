#!/usr/bin/env python3
"""MPR A/B harness — "MPR on/off" over a curated query set (Spec 08 §3).

In the style of ``deploy/spark/ctx_harness.py``: **stdlib-only** (urllib/json/argparse/re/pathlib), drives
REAL ``/chat`` turns against the orchestrator (:8100) and reads perf from the ``[perf]`` line of the response
(``TTFT {s}s · {ct} tok/{gt}s = {rate} tok/s · prompt {n}`` — verified gx10.py:2206-2213). The
**pure** functions (extract_perf/estimate_cost/diff_arms/assert_off_byte_identical/load_*/write_report)
are deterministic and covered by ``--selftest`` + the pytest gate; the **live A/B** (run_arm/main)
costs tokens and does NOT run in the pytest gate, but before the merge (Spec 08 §7 stage 4).

Secret-free: the deployment secret comes ONLY via ``--token`` (or is absent); the Spark address via
``--base`` — no host/token literal in the code.

Note (like ctx_harness): the MPR posture (on/off) is set by the **server/build** that ``--base``
points to — the HTTP API has no per-request switch. The ``mpr`` parameter of ``run_arm`` is therefore
the **arm label** (A=on against ``--base``, B=off against ``--base-off``), not a server toggle.

Usage:
  python skills/mpr/eval/harness.py --base http://localhost:8100 --base-off http://localhost:8101 \
      --set skills/mpr/eval/sets/architecture_decision.jsonl \
      --refs skills/mpr/eval/refs/architecture_decision.refs.json --token <secret> --out runs/ab/arch/
  python skills/mpr/eval/harness.py --selftest          # pure functions, no network
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

# Perf line (gx10.py:2206-2213). prompt: last match; completion: first `N tok` (= {ct} tok/{gt}s,
# like cli.py:306 _TOK_RE — the rate `K tok/s` would only come after that); ttft: `TTFT {s}s`.
_PROMPT_RE = re.compile(r"prompt\s+(\d+)")
_TTFT_RE = re.compile(r"TTFT\s+(\d+(?:\.\d+)?)\s*s")   # strict float → no malformed-capture ValueError
_TOK_RE = re.compile(r"(\d+)\s*tok")

REPORT_OPEN = "<<<MPR_REPORT>>>"
REPORT_CLOSE = "<<<END>>>"


# ── pure measurement/aggregation functions (in --selftest + pytest gate) ────────────────────────────
def extract_perf(output: Optional[str]) -> Dict[str, Optional[float]]:
    """``{prompt_tokens, ttft_s, completion_tokens}`` from the ``[perf]`` line. Missing field → None;
    no perf line → all None. Pure → unit-tested."""
    s = str(output or "")
    pm = _PROMPT_RE.findall(s)
    tm = _TTFT_RE.search(s)
    cm = _TOK_RE.search(s)
    return {
        "prompt_tokens": int(pm[-1]) if pm else None,
        "ttft_s": float(tm.group(1)) if tm else None,
        "completion_tokens": int(cm.group(1)) if cm else None,
    }


def estimate_cost(perspectives: List[dict], pricing: Dict[str, dict]) -> float:
    """Σ per perspective of ``prompt/1k·in + completion/1k·out`` with the provider-pool price list
    (``cost_per_1k_in_usd``/``cost_per_1k_out_usd``). Pure. Unknown provider/missing tokens →
    0 contribution (never crashes). local-only (cost 0) contributes 0."""
    total = 0.0
    for p in perspectives or []:
        price = (pricing or {}).get(p.get("provider", ""), {}) or {}
        tok = p.get("tokens") or {}
        in_tok = tok.get("prompt") or 0
        out_tok = tok.get("completion") or 0
        total += (in_tok / 1000.0) * float(price.get("cost_per_1k_in_usd", 0.0) or 0.0)
        total += (out_tok / 1000.0) * float(price.get("cost_per_1k_out_usd", 0.0) or 0.0)
    return round(total, 6)


def _sub(x: Optional[float], y: Optional[float]) -> Optional[float]:
    return round(x - y, 6) if (x is not None and y is not None) else None


def diff_arms(a: List[dict], b: List[dict]) -> Dict[str, dict]:
    """Pairwise A/B comparison per ``id``: answers + perf/cost/latency deltas (A−B). Pure."""
    bi = {r.get("id"): r for r in (b or [])}
    out: Dict[str, dict] = {}
    for ra in a or []:
        rb = bi.get(ra.get("id"), {})
        pa, pb = (ra.get("perf") or {}), (rb.get("perf") or {})
        out[ra.get("id")] = {
            "query": ra.get("query"), "domain": ra.get("domain"),
            "a_answer": ra.get("answer"), "b_answer": rb.get("answer"),
            "a_ok": ra.get("ok"), "b_ok": rb.get("ok"),
            "a_cost_usd": ra.get("cost_usd"), "b_cost_usd": rb.get("cost_usd"),
            "cost_delta_usd": _sub(ra.get("cost_usd"), rb.get("cost_usd")),
            "a_prompt_tokens": pa.get("prompt_tokens"), "b_prompt_tokens": pb.get("prompt_tokens"),
            "a_completion_tokens": pa.get("completion_tokens"), "b_completion_tokens": pb.get("completion_tokens"),
            "latency_delta_s": _sub(ra.get("latency_s"), rb.get("latency_s")),
        }
    return out


def assert_off_byte_identical(off: List[dict], plain: List[dict]) -> None:
    """§3.3(2): "MPR off == MPR-free build" per ``id`` (deterministic sampling, temp=0). On drift
    → AssertionError (gate fail). Pure. FAIL-CLOSED: an empty arm, duplicate ids OR a failed
    turn on one side do NOT count as parity (otherwise the strongest regression gate goes green vacuously)."""
    off, plain = off or [], plain or []
    assert off and plain, "byte-identity gate ran on an empty arm — nothing compared"
    oa = {r.get("id"): r for r in off}
    pa = {r.get("id"): r for r in plain}
    assert len(oa) == len(off) and len(pa) == len(plain), "duplicate query ids — ambiguous comparison"
    assert oa.keys() == pa.keys(), f"arm id mismatch: {sorted(set(oa) ^ set(pa))}"
    drift = []
    for i in oa:
        ro, rp = oa[i], pa[i]
        if not (ro.get("ok") and rp.get("ok")):
            drift.append(i)                       # a failed turn can never be claimed byte-identical
        elif ro.get("answer") != rp.get("answer"):
            drift.append(i)
    assert not drift, f"MPR-off weicht vom MPR-freien Build ab (oder Turn fehlgeschlagen) für: {sorted(set(drift))}"


def load_set(path: str) -> List[dict]:
    """jsonl eval set → ``[{id, query, domain, route_hint?}]`` (blank lines skipped). Pure-ish (I/O)."""
    out: List[dict] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def load_refs(path: str) -> Dict[str, dict]:
    """Reference dimension lists per ``query_id`` (json)."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _render_md(diff: Dict[str, dict]) -> str:
    lines = ["# MPR A/B-Report", "",
             "| query_id | Domäne | A ok | B ok | Δ prompt-tok | Δ compl-tok | Δ Kosten $ | Δ Latenz s |",
             "|---|---|:--:|:--:|--:|--:|--:|--:|"]
    for qid in sorted(diff):
        d = diff[qid]
        dp = _sub(d.get("a_prompt_tokens"), d.get("b_prompt_tokens"))
        dc = _sub(d.get("a_completion_tokens"), d.get("b_completion_tokens"))
        lines.append(f"| {qid} | {d.get('domain') or '–'} | {d.get('a_ok')} | {d.get('b_ok')} | "
                     f"{dp if dp is not None else '–'} | {dc if dc is not None else '–'} | "
                     f"{d.get('cost_delta_usd') if d.get('cost_delta_usd') is not None else '–'} | "
                     f"{d.get('latency_delta_s') if d.get('latency_delta_s') is not None else '–'} |")
    return "\n".join(lines) + "\n"


def write_report(diff: Dict[str, dict], out_dir: str) -> str:
    """``report.json`` (sorted, git-diffable) + ``report.md``. Returns the json path."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    jp = out / "report.json"
    jp.write_text(json.dumps(diff, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    (out / "report.md").write_text(_render_md(diff), encoding="utf-8")
    return str(jp)


# ── live layer (NOT in the pytest gate; only live A/B before merge) ──────────────────────────────────
def _auth(token: Optional[str]) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def chat_turn(base: str, message: str, token: Optional[str] = None, timeout: float = 120.0) -> Dict[str, Any]:
    body = json.dumps({"message": message}).encode("utf-8")
    headers = {"Content-Type": "application/json", **_auth(token)}
    req = urllib.request.Request(base.rstrip("/") + "/chat", data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def _manifest_perspectives(resp: Dict[str, Any], manifest_ref: Any,
                           runs_dir: Optional[str]) -> Optional[List[dict]]:
    """Best-effort perspectives for cost: inline from the /chat response, else from the run manifest on
    disk (``runs_dir/<run_id>/manifest.json`` or *manifest_ref* as a direct path). Fail-soft → None."""
    inline = resp.get("perspectives")
    if isinstance(inline, list):
        return inline
    if not (manifest_ref and runs_dir):
        return None
    for cand in (Path(runs_dir) / str(manifest_ref) / "manifest.json", Path(str(manifest_ref))):
        try:
            return json.loads(cand.read_text(encoding="utf-8")).get("perspectives")
        except Exception:  # noqa: BLE001 — manifest not reachable from here → cost stays None
            continue
    return None


def run_arm(base: str, queries: List[dict], *, mpr: bool, token: Optional[str] = None,
            timeout: float = 120.0, pricing: Optional[Dict[str, dict]] = None,
            runs_dir: Optional[str] = None) -> List[dict]:
    """One arm (A=mpr on / B=baseline) over the query set. The MPR posture is set by the server behind
    *base*; *mpr* is the arm label in the result. Fail-soft per query (turn error → ok=False, continue).
    If *pricing* + (inline perspectives OR *runs_dir*) are available, ``cost_usd`` is estimated from the
    manifest provenance (estimate_cost); otherwise it stays None."""
    out: List[dict] = []
    for q in queries:
        t0 = time.monotonic()
        rec: Dict[str, Any] = {"id": q.get("id"), "query": q.get("query"), "domain": q.get("domain"),
                               "mpr": bool(mpr), "ok": True, "answer": None, "perf": {},
                               "latency_s": None, "manifest_ref": None, "cost_usd": None, "error": None}
        try:
            resp = chat_turn(base, q.get("query", ""), token, timeout)
            rec["answer"] = str(resp.get("output", ""))
            rec["perf"] = extract_perf(rec["answer"])
            rec["manifest_ref"] = resp.get("manifest") or resp.get("run_id")
            if pricing:
                persps = _manifest_perspectives(resp, rec["manifest_ref"], runs_dir)
                if persps is not None:
                    rec["cost_usd"] = estimate_cost(persps, pricing)
        except Exception as e:  # noqa: BLE001 — fail-soft observation, continue the arm
            rec["ok"], rec["error"] = False, repr(e)
        rec["latency_s"] = round(time.monotonic() - t0, 3)
        out.append(rec)
    return out


def _selftest() -> None:
    """Pure-function checks, NO network (Spec 08 §7 stage 3)."""
    p = extract_perf("[perf] TTFT 0.5s · 120 tok/2.0s = 60 tok/s · prompt 2175")
    assert p == {"prompt_tokens": 2175, "ttft_s": 0.5, "completion_tokens": 120}, p
    assert extract_perf("a prompt 10 b prompt 20")["prompt_tokens"] == 20      # last prompt wins
    assert extract_perf("keine perf") == {"prompt_tokens": None, "ttft_s": None, "completion_tokens": None}
    assert extract_perf(None)["prompt_tokens"] is None
    pricing = {"claude-sonnet": {"cost_per_1k_in_usd": 0.003, "cost_per_1k_out_usd": 0.015},
               "spark-vllm": {"cost_per_1k_in_usd": 0.0, "cost_per_1k_out_usd": 0.0}}
    persps = [{"provider": "claude-sonnet", "tokens": {"prompt": 1000, "completion": 1000}},
              {"provider": "spark-vllm", "tokens": {"prompt": 2000, "completion": 500}},
              {"provider": "unknown", "tokens": {"prompt": 9999, "completion": 9999}}]
    assert estimate_cost(persps, pricing) == 0.018, estimate_cost(persps, pricing)  # local + unknown → 0
    assert estimate_cost([], pricing) == 0.0
    a = [{"id": "q1", "answer": "X", "ok": True, "cost_usd": 0.02, "latency_s": 3.0,
          "perf": {"prompt_tokens": 100, "completion_tokens": 50}}]
    b = [{"id": "q1", "answer": "Y", "ok": True, "cost_usd": 0.0, "latency_s": 1.0,
          "perf": {"prompt_tokens": 80, "completion_tokens": 40}}]
    d = diff_arms(a, b)["q1"]
    assert d["a_answer"] == "X" and d["b_answer"] == "Y"
    assert d["cost_delta_usd"] == 0.02 and d["latency_delta_s"] == 2.0
    ok = lambda i, a: {"id": i, "answer": a, "ok": True}        # noqa: E731 — terse selftest helper
    assert_off_byte_identical([ok("q1", "same")], [ok("q1", "same")])
    for bad_off, bad_plain in (([ok("q1", "a")], [ok("q1", "b")]),        # drift
                               ([], []),                                   # empty → vacuous, must raise
                               ([{"id": "q1", "answer": None, "ok": False}],
                                [{"id": "q1", "answer": None, "ok": False}])):  # mutual failure ≠ parity
        try:
            assert_off_byte_identical(bad_off, bad_plain)
        except AssertionError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"assert_off_byte_identical must raise on {bad_off!r}/{bad_plain!r}")
    assert _manifest_perspectives({"perspectives": [{"provider": "x"}]}, None, None) == [{"provider": "x"}]
    assert _manifest_perspectives({}, None, None) is None
    print("harness selftest: OK")


def main() -> None:
    ap = argparse.ArgumentParser(description="MPR A/B-Harness — MPR on/off über eine Query-Menge")
    ap.add_argument("--base", default="http://localhost:8100", help="Orchestrator-Base (MPR-on-Arm)")
    ap.add_argument("--base-off", default=None, help="Baseline-Base (MPR-off-Build); aktiviert den A/B-Diff")
    ap.add_argument("--base-free", default=None, help="MPR-freier Build; aktiviert das §3.3(2) off==free-Gate")
    ap.add_argument("--set", dest="set_path", default=None, help="Eval-Set jsonl")
    ap.add_argument("--refs", default=None, help="Referenz-Dimensionen json (für den Judge, Ev-5)")
    ap.add_argument("--token", default=None, help="Deployment-Secret für token/sealed-Profile")
    ap.add_argument("--out", default="runs/ab/", help="Report-Ausgabeverzeichnis")
    ap.add_argument("--runs-dir", default=None, help="lokales runs/mpr/ (falls erreichbar) → Kosten aus Manifest")
    ap.add_argument("--pricing", default=None, help="json mit dem Provider-Pool (cost_per_1k_in/out_usd) → Kosten")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--selftest", action="store_true", help="reine Funktionen, kein Netz")
    args = ap.parse_args()
    if args.selftest:
        _selftest()
        return
    if not args.set_path:
        ap.error("--set ist erforderlich (oder --selftest)")
    queries = load_set(args.set_path)
    pricing = json.loads(Path(args.pricing).read_text(encoding="utf-8")) if args.pricing else None
    arm = lambda base, mpr: run_arm(base, queries, mpr=mpr, token=args.token, timeout=args.timeout,
                                    pricing=pricing, runs_dir=args.runs_dir)  # noqa: E731
    a = arm(args.base, True)
    if args.base_off:
        b = arm(args.base_off, False)
        print(f"A/B-Report → {write_report(diff_arms(a, b), args.out)}")
        if args.base_free:                                   # §3.3(2) strongest regression gate
            free = arm(args.base_free, False)
            try:
                assert_off_byte_identical(b, free)
                print("§3.3(2) off==free: PASS")
            except AssertionError as e:
                print(f"§3.3(2) off==free: FAIL — {e}")
    else:
        Path(args.out).mkdir(parents=True, exist_ok=True)
        p = Path(args.out) / "arm_a.json"
        p.write_text(json.dumps(a, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Arm A (MPR on) → {p}  (kein --base-off → kein A/B-Diff)")


if __name__ == "__main__":
    main()
