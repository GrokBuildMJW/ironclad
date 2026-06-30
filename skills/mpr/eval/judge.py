#!/usr/bin/env python3
"""MPR LLM-Judge-Panel (Spec 08 §5.2) — 3 blind votes, structured JSON, self-consistency, rubric-aggregated.

The LLM call is INJECTED (``call(prompt, *, system, max_tokens) -> str``), the SAME doctrine as
synthesis.py's ``llm_call`` and harness.py — so the panel is decoupled, deterministic and stub-testable
(no network in the gate). The live judge binds a real provider-CLI/engine call per ``mpr.providers`` entry;
the gate exercises only the pure parsing/blinding/aggregation via ``--selftest`` + test_judge.py.

Bias mitigation (§5.2): blind A/B (two anonymised answers in a seeded-random order, de-blindable),
3 votes from DIFFERENT configured providers (median per dimension → robust), pairwise + absolute, and
self-consistency (each voice twice at temp=0 → an unstable voice is dropped). Structured per-dimension
JSON {score, rationale, cited_axes} + a pairwise winner — parsed with exactly ONE repair re-ask, then the
voice is dropped (never a free-text parse). Aggregation rides rubric.median_scores (no duplicate stats).
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))   # eval/ on path → `import rubric` (script + importlib)
from rubric import JUDGED_DIMS, median_scores  # noqa: E402

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)

SYSTEM = (
    "You are an impartial evaluator. You are given TWO anonymised answers to the same question "
    "(Answer 1, Answer 2) plus a reference axis list (ground truth). You do NOT know which system "
    "produced which answer — do NOT guess. Rate each answer per dimension 0-5, justify briefly and "
    "name the covered reference axes. Emit ONLY ONE ```json block per the schema."
)


def build_prompt(answer_1: str, answer_2: str, axes: List[str]) -> str:
    dims = ", ".join(JUDGED_DIMS)
    skel = ('{"answer_1":{"<dim>":{"score":0-5,"rationale":"...","cited_axes":["..."]}},'
            '"answer_2":{"<dim>":{...}},"pairwise":{"<dim>":"1"|"2"}}')
    return (
        f"REFERENCE AXES (ground truth): {axes}\n\nDIMENSIONS: {dims}\n\n"
        f"--- Answer 1 ---\n{answer_1}\n\n--- Answer 2 ---\n{answer_2}\n\n"
        f"Rate BOTH answers per dimension (0-5) + pairwise (which is better per dimension, '1' or "
        f"'2'). TARGET SCHEMA (```json first):\n{skel}"
    )


def _extract_json(raw: str) -> Optional[dict]:
    """First JSON object in a reply: whole-string → fenced → raw_decode scan from each '{'. The scan
    handles a brace-in-prose preamble ('Note {x}\\n{...}') and trailing text WITHOUT the first-'{'..last-'}'
    over-span bug, and is nesting-safe (a non-greedy fence regex would wrongly stop at the first '}')."""
    s = (raw or "").strip()
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:  # noqa: BLE001
        pass
    m = _FENCE_RE.search(s)
    if m:
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                return obj
        except Exception:  # noqa: BLE001
            pass
    dec = json.JSONDecoder()
    idx = s.find("{")
    while idx != -1:
        try:
            obj, _ = dec.raw_decode(s, idx)
            if isinstance(obj, dict):
                return obj
        except Exception:  # noqa: BLE001
            pass
        idx = s.find("{", idx + 1)
    return None


def _valid_side(side: Any) -> Optional[Dict[str, float]]:
    """A per-answer block must score every judged dim with a number in [0,5]. → {dim: score} or None."""
    if not isinstance(side, dict):
        return None
    out: Dict[str, float] = {}
    for d in JUDGED_DIMS:
        cell = side.get(d)
        sc = cell.get("score") if isinstance(cell, dict) else cell
        try:
            sc = float(sc)
        except (TypeError, ValueError):
            return None
        if not (0.0 <= sc <= 5.0):
            return None
        out[d] = sc
    return out


def parse_judgement(raw: str) -> Optional[dict]:
    """Parse + validate the blind judgement → {'1': {dim:score}, '2': {dim:score}, 'pairwise': {dim:'1'|'2'}}.
    None on any shape violation (caller re-asks once, then drops the voice). Pure."""
    obj = _extract_json(raw)
    if obj is None:
        return None
    s1, s2 = _valid_side(obj.get("answer_1")), _valid_side(obj.get("answer_2"))
    if s1 is None or s2 is None:
        return None
    pw_in = obj.get("pairwise")
    pw_in = pw_in if isinstance(pw_in, dict) else {}   # MPR-EVAL-1 (#503): a non-dict pairwise (e.g. a str)
                                                       # must not be indexed — drop it wholesale, never raise.
    # MPR-EVAL-1 (#503): keep ONLY a valid '1'/'2' pairwise vote. Coercing any other value (junk, '3',
    # missing/None) to '2' biased the blind panel toward slot 2 — a malformed dim is dropped, not voted.
    pairwise = {d: str(pw_in[d]) for d in JUDGED_DIMS if d in pw_in and str(pw_in[d]) in ("1", "2")}
    return {"1": s1, "2": s2, "pairwise": pairwise}


def _run_vote(answer_a: str, answer_b: str, axes: List[str], *, call: Callable[..., str],
              flipped: bool, max_tokens: int = 1500) -> Optional[dict]:
    """ONE vote at a GIVEN blind order → call (+1 repair re-ask) → parse → DE-BLIND back to a/b.
    Returns {'a': {dim:score}, 'b': {dim:score}, 'pairwise': {dim:'a'|'b'}, 'flipped': bool} or None."""
    shown_1, shown_2 = (answer_b, answer_a) if flipped else (answer_a, answer_b)
    prompt = build_prompt(shown_1, shown_2, axes)
    parsed = parse_judgement(call(prompt, system=SYSTEM, max_tokens=max_tokens))
    if parsed is None:                                 # §5/§4.4: exactly ONE repair re-ask, then drop
        reask = prompt + "\n\nYour previous output was NOT schema-valid. Emit ONLY the ```json block."
        parsed = parse_judgement(call(reask, system=SYSTEM, max_tokens=max_tokens))
    if parsed is None:
        return None
    a_slot, b_slot = ("2", "1") if flipped else ("1", "2")   # slot '1' is b iff flipped
    deblind_pw = {d: ("a" if w == a_slot else "b") for d, w in parsed["pairwise"].items()}
    return {"a": parsed[a_slot], "b": parsed[b_slot], "pairwise": deblind_pw, "flipped": flipped}


def judge_vote(answer_a: str, answer_b: str, axes: List[str], *, call: Callable[..., str],
               rng: random.Random, max_tokens: int = 1500) -> Optional[dict]:
    """ONE blind vote at a seeded-random A/B order (de-blinded back to a/b)."""
    return _run_vote(answer_a, answer_b, axes, call=call, flipped=(rng.random() < 0.5), max_tokens=max_tokens)


def self_consistent_vote(answer_a: str, answer_b: str, axes: List[str], *, call: Callable[..., str],
                         rng: random.Random, tol: float = 1.0, max_tokens: int = 1500) -> Optional[dict]:
    """Each voice TWICE at the SAME blind order (order decided once) → drop if unstable (any per-dim |Δ| >
    tol on either answer) or unparsable. Fixing the order isolates temp-instability from position-bias —
    a (legitimately) position-biased judge must not be dropped merely because the order was re-randomised."""
    flipped = rng.random() < 0.5
    v1 = _run_vote(answer_a, answer_b, axes, call=call, flipped=flipped, max_tokens=max_tokens)
    v2 = _run_vote(answer_a, answer_b, axes, call=call, flipped=flipped, max_tokens=max_tokens)
    if v1 is None or v2 is None:
        return None
    for side in ("a", "b"):
        for d in JUDGED_DIMS:
            if abs(v1[side][d] - v2[side][d]) > tol:    # direct index: _valid_side guarantees every dim
                return None                             # unstable voice → dropped (quality guard)
    return v1


def judge_panel(answer_a: str, answer_b: str, axes: List[str], *,
                voices: List[Dict[str, Any]], rng: random.Random) -> dict:
    """Run one self-consistent vote per configured voice (``{'provider': name, 'call': fn}``), aggregate by
    MEDIAN per dimension (rubric) + pairwise MAJORITY. Provider list comes from config, never hardcoded.

    CONTRACT: an all-dropped panel returns ``n_votes == 0`` with empty ``a``/``b``/``pairwise`` (never
    raises — fail-soft). Callers MUST treat ``n_votes == 0`` as a HARD FAIL, not 'tie / no signal'
    (rubric.passes is already fail-closed on the resulting missing dims). Pairwise tie → ``'a'`` (the
    de-blinded arm-A default); a dimension no kept voice scored pairwise is omitted."""
    kept, used, dropped = [], [], []
    for v in voices:
        vote = self_consistent_vote(answer_a, answer_b, axes, call=v["call"], rng=rng)
        if vote is None:
            dropped.append(v["provider"])
        else:
            vote = dict(vote, provider=v["provider"])
            kept.append(vote)
            used.append(v["provider"])
    a = median_scores([k["a"] for k in kept])
    b = median_scores([k["b"] for k in kept])
    pairwise = {}
    for d in JUDGED_DIMS:
        wins_a = sum(1 for k in kept if k["pairwise"].get(d) == "a")
        wins_b = sum(1 for k in kept if k["pairwise"].get(d) == "b")
        if wins_a or wins_b:
            pairwise[d] = "a" if wins_a >= wins_b else "b"   # tie → 'a' (documented arm-A default)
    return {"a": a, "b": b, "pairwise": pairwise, "providers_used": used,
            "dropped_providers": dropped, "n_votes": len(kept), "votes": kept}


def _selftest() -> None:
    """Pure parse/blind/aggregation checks, NO network (gate §7 stage 3)."""
    good = json.dumps({
        "answer_1": {d: {"score": 4, "rationale": "ok", "cited_axes": ["x"]} for d in JUDGED_DIMS},
        "answer_2": {d: {"score": 2, "rationale": "ok", "cited_axes": []} for d in JUDGED_DIMS},
        "pairwise": {d: "1" for d in JUDGED_DIMS}})
    p = parse_judgement(good)
    assert p and p["1"]["coverage"] == 4.0 and p["pairwise"]["coverage"] == "1"
    assert parse_judgement("kein json") is None
    assert parse_judgement(json.dumps({"answer_1": {"coverage": {"score": 9}}})) is None   # out of range
    # de-blind: a fixed-flip rng must map slots back to a/b correctly
    fixed = lambda prompt, *, system, max_tokens: good                       # noqa: E731

    class _NoFlip:                       # random()>=0.5 → never flip → slot-scoring stubs stay deterministic
        def random(self):
            return 0.9
    v = judge_vote("A", "B", ["x"], call=fixed, rng=_NoFlip())
    assert v and set(v["a"]) == set(JUDGED_DIMS) and v["pairwise"]["coverage"] in ("a", "b")
    # panel: 3 stub voices → median; one unstable voice dropped
    stable = {"provider": "p1", "call": fixed}
    flip = {"flag": False}

    def _unstable(prompt, *, system, max_tokens):
        flip["flag"] = not flip["flag"]
        sc = 5 if flip["flag"] else 0
        return json.dumps({"answer_1": {d: {"score": sc} for d in JUDGED_DIMS},
                           "answer_2": {d: {"score": 2} for d in JUDGED_DIMS},
                           "pairwise": {d: "1" for d in JUDGED_DIMS}})
    panel = judge_panel("A", "B", ["x"], voices=[stable, {"provider": "p2", "call": fixed},
                                                 {"provider": "bad", "call": _unstable}],
                        rng=_NoFlip())
    assert panel["n_votes"] == 2 and "bad" in panel["dropped_providers"]      # unstable voice dropped
    assert panel["a"]["coverage"] == 4.0                                      # median of the two stable
    print("judge selftest: OK")


def main() -> None:  # pragma: no cover - operator/live path, not in the pytest gate
    ap = argparse.ArgumentParser(description="MPR LLM judge panel over an A/B report")
    ap.add_argument("--report", help="report.json from harness.py (holds a/b answers per query_id)")
    ap.add_argument("--refs", default=None, help="reference-axes json per query_id")
    ap.add_argument("--panel", default="sonnet,opus,spark", help="provider panel (from mpr.providers)")
    ap.add_argument("--seed", type=int, default=0, help="blind-order seed (deterministic/de-blindable)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        _selftest()
        return
    ap.error("a live judge run needs --report + wired providers (mpr.providers); the gate uses --selftest only.")


if __name__ == "__main__":
    main()
