"""Cross-verify — deterministic conflict detection (Spec 06 §3). Signal, not smoothing.

Conflicts are the most valuable product of a panel: this detector *locates* the friction (it never
decides who is right — the synthesis prompt does that, §5), and feeds the synthesis a mandatory
"conflict zones" section. Four LLM-free sub-detectors, each fail-soft (a throwing sub-detector yields
[] for itself, never aborts the stage); results merged by (kind, topic), severity = max, sorted
blocking-first.

``detect_conflicts`` takes the OK perspectives duck-typed (anything with ``.role`` + ``.content``) so it
is buildable + testable before synthesis.py exists and never imports it (no cycle). ``subjects``/``mode``
are the §3.1 context (options from the router / query entities); omitted → inferred from the contents
(those inferred subjects yield ``minor`` polarity — "secondary subject", §3.2 severity rule).

Detection quality (hardened after adversarial review):
* polarity reads negation per *clause* around the subject, not sentence-wide, and never lets the first
  clause `break` suppress a later stance;
* numeric groups by (subject, unit) when subjects are known (so "Latenz 5ms" ≠ "Timeout 100ms"), only
  collapsing to unit alone when no subject context exists;
* recommendation/subject matching is word-boundary (so "Postgres" ≠ "PostgresQL-Cluster") and the
  reco top-option is the earliest in the sentence;
* claim only fires when the negation hits a token in the *shared core* (so "…nicht kritisch" doesn't
  contradict a claim about latency), and only compares negated-vs-non-negated sentences (smaller than
  the full O(n²) cross-product), capped per perspective.
"""
from __future__ import annotations

import re
import statistics
from typing import Any, List, Optional, Protocol, Sequence

from pydantic import BaseModel, ConfigDict

DEFAULT_NUMERIC_SPREAD = 0.25  # mpr.conflict_numeric_spread (§9)
_MAX_SENTS = 60                # per-perspective sentence cap for the claim cross-product (DoS guard)


class _HasContent(Protocol):
    role: str
    content: Optional[str]


# ── schema (§3.1) ─────────────────────────────────────────────────────────────────────────────────
class ConflictSide(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roles: List[str]                 # roles holding this side
    stance: str                      # the extracted core claim
    evidence: Optional[str] = None   # a span/quote from the perspective output


class Conflict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str                        # claim | recommendation | numeric | polarity
    topic: str                       # short label
    sides: List[ConflictSide]        # >=2 opposing camps
    severity: str                    # minor | material | blocking
    detector: str                    # which sub-detector fired (audit/tests)


# ── lexicons (DE+EN) ────────────────────────────────────────────────────────────────────────────
_RECO_RE = re.compile(
    r"\b(empfehl\w*|recommend\w*|sollte[n]?|should|go\s+with|w(ä|ae)hl\w*|bevorzug\w*|"
    r"prefer\w*|best\s+choice|beste\s+wahl)\b", re.IGNORECASE)
_NEG_RE = re.compile(
    r"\b(nicht|kein\w*|no|not|never|cannot|can'?t|don'?t|avoid|vermeide\w*|gegen|against)\b",
    re.IGNORECASE)
# units must be UNAMBIGUOUS — bare "x" (multiplier), "k"/"m" (scale) produced noise conflict topics
# ("SQL x", "… k") with no real meaning, so they're excluded; concrete units stay (LB-7).
_NUM_RE = re.compile(
    r"(?P<num>\d[\d.,]*)\s?(?P<unit>%|€|\$|ms|gb|tb|mb|tok/s|tokens?/s|req/s|qps)\b",
    re.IGNORECASE)
# split on sentence punctuation, but NOT a dot between digits — else a version/decimal like
# "Qwen3.6" or "3.14" splits into a "6 35B A3B" fragment with a dangling quote (LOK-12).
_SENT_RE = re.compile(r"[!?\n]+|(?<!\d)\.(?!\d)")
# clause boundaries so negation/reco are read in the clause that mentions the subject, not the whole sentence
_CLAUSE_RE = re.compile(
    r"[,;:]|\b(weil|aber|denn|jedoch|obwohl|w(ä|ae)hrend|sondern|because|but|however|although|while)\b",
    re.IGNORECASE)
_WORD_RE = re.compile(r"[a-zA-Zäöüß0-9]+")
_STOP = frozenset({
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "und", "oder", "ist", "sind", "war",
    "wird", "werden", "im", "in", "auf", "an", "als", "bei", "aus", "für", "mit", "von", "zu", "man",
    "es", "sich", "the", "a", "an", "and", "or", "is", "are", "be", "to", "of", "for", "with", "on",
    "in", "at", "as", "this", "that", "it",
    # question words — a question opens with one, capitalised at sentence start, and would otherwise
    # become a bogus subject ("pro Sollte ↔ contra Sollte"); #55.
    "sollte", "soll", "sollen", "wie", "was", "warum", "welche", "welcher", "welches", "wann", "wer",
    "wo", "wofür", "kann", "können", "muss", "müssen", "ob",
    "should", "could", "would", "how", "what", "why", "which", "when", "who", "where", "can", "must",
    "whether", "do", "does",
})

# MPR structure/meta vocabulary that appears in the QUERY (the instruction) but is never a decision
# subject — excluded from subject inference so "pro Entscheidungsmatrix ↔ contra Entscheidungsmatrix"
# (and a fake "top: Empfehlung ↔ top: Entscheidungsmatrix" recommendation conflict) can't arise (#55).
_META_TERMS = frozenset({
    "entscheidungsmatrix", "matrix", "empfehlung", "empfehlungen", "rückzugsoption", "rückzugsoptionen",
    "option", "optionen", "vergleich", "vergleichsmatrix", "analyse", "bewertung", "kriterium",
    "kriterien", "score", "scores", "dimension", "dimensionen", "auslöser", "fazit",
    "decision", "recommendation", "fallback", "options", "comparison", "analysis", "criterion",
    "criteria", "evidence", "report", "summary",
})

_SEV_RANK = {"blocking": 0, "material": 1, "minor": 2}

# a claim stance/evidence shown in the report is a RAW model sentence → strip list/inline markdown
# and trim stray quotes so a conflict line reads cleanly, not "- **Warum nicht „35B A3B"" (LOK-12).
_LIST_PREFIX_RE = re.compile(r"^[\s>]*(?:[-*+]\s+|\d+[.)]\s+)+")
_MD_INLINE_RE = re.compile(r"\*\*|__|\*|`|~~|#{1,6}\s+")
_SPAN_MAX = 140


def _clean_span(text: str) -> str:
    s = re.sub(r"\s+", " ", (text or "")).strip()
    s = _LIST_PREFIX_RE.sub("", s)
    s = _MD_INLINE_RE.sub("", s)
    s = s.strip(" \t\"'„“”‚‘’`*_-")
    return (s[:_SPAN_MAX].rstrip() + "…") if len(s) > _SPAN_MAX else s


def _sentences(text: str) -> List[str]:
    return [s.strip() for s in _SENT_RE.split(text or "") if s.strip()]


def _clauses(sent: str) -> List[str]:
    return [c.strip() for c in _CLAUSE_RE.sub("|", sent or "").split("|") if c.strip()]


def _content_tokens(text: str) -> set:
    # negation words are tracked separately (not in the content set) so "X" vs "not X" share content.
    return {w for w in (t.lower() for t in _WORD_RE.findall(text or ""))
            if len(w) >= 3 and w not in _STOP and not _NEG_RE.fullmatch(w)}


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _word_pat(subject: str) -> re.Pattern:
    return re.compile(r"(?<![\wäöüß])" + re.escape(subject.lower()) + r"(?![\wäöüß])")


def _mentions(text: str, subject: str) -> bool:
    return bool(_word_pat(subject).search((text or "").lower()))


def _count_mentions(text: str, subject: str) -> int:
    return len(_word_pat(subject).findall((text or "").lower()))


def _subject_for_number(sent: str, subjects: List[str], pos: int) -> Optional[str]:
    """The subject a number at *pos* belongs to: the nearest subject mentioned BEFORE it in the
    sentence (so "Latenz 5ms, Timeout 100ms" attributes each number to its own subject), else the
    nearest overall."""
    low = (sent or "").lower()
    cands = [(low.find(s.lower()), s) for s in subjects if _mentions(sent, s)]
    if not cands:
        return None
    before = [(i, s) for i, s in cands if i <= pos]
    if before:
        return max(before, key=lambda c: c[0])[1]
    return min(cands, key=lambda c: abs(c[0] - pos))[1]


def _parse_num(s: str) -> Optional[float]:
    s = s.strip()
    try:
        if "," in s:                       # DE decimal: dot = thousands, comma = decimal
            return float(s.replace(".", "").replace(",", "."))
        if "." in s:
            _, _, frac = s.partition(".")
            if s.count(".") == 1 and len(frac) == 3:   # "1.000" → thousands, not 1.0
                return float(s.replace(".", ""))
            return float(s)
        return float(s)
    except ValueError:
        return None


def _dedupe_substrings(subjects: List[str]) -> List[str]:
    """Drop a subject that is contained in a more specific one (e.g. "Containern" ⊂ "Docker-Containern")
    so fragmented duplicates don't each raise their own conflict zone (#55). Longest kept first."""
    kept: List[str] = []
    for s in sorted(subjects, key=len, reverse=True):  # most-specific (longest) first
        sl = s.lower()
        if any(sl != k.lower() and sl in k.lower() for k in kept):
            continue
        kept.append(s)
    return kept


def _infer_subjects(ok: Sequence[Any], query: str = "") -> List[str]:
    # best-effort: capitalised multi-char tokens shared by >=2 perspectives. In German EVERY noun is
    # capitalised, so this over-generates (e.g. "Kosten", "API", "Risiko") → bogus polarity conflicts
    # ("pro Kosten ↔ contra Kosten"). When the query is known, anchor to it: a real decision subject
    # (the options being weighed) is named in the question; generic nouns are not (Live-Bug #4).
    from collections import Counter
    counts: Counter = Counter()
    for p in ok:
        counts.update({w for w in re.findall(r"\b[A-ZÄÖÜ][\wäöüß-]{2,}\b", p.content or "")})
    # drop stopwords/question-words (LB-7/#55) AND MPR meta/structure terms — the latter appear in the
    # query instruction ("Erstelle eine Entscheidungsmatrix …") but are never decision subjects (#55).
    cands = [w for w, n in counts.items()
             if n >= 2 and w.lower() not in _STOP and w.lower() not in _META_TERMS]
    if query:
        # Anchor to the QUESTION part — the text before the first '?'. The options being weighed are named
        # in the question; criteria + meta ("entlang Stabilität, Sicherheit …", "mit Empfehlung und
        # Rückzugsoption") sit in the instruction tail and must not become subjects (#55). No '?' → whole query.
        q = query.split("?", 1)[0] if "?" in query else query
        ql = q.lower()
        cands = [w for w in cands if re.search(r"\b" + re.escape(w.lower()) + r"\b", ql)]
    return _dedupe_substrings(cands)


# ── sub-detectors (signature: ok, subjects, mode, spread, provided) ───────────────────────────────
def _polarity(ok, subjects, mode, spread, provided) -> List[Conflict]:
    out: List[Conflict] = []
    for subject in subjects:
        pro, contra = [], []
        for p in ok:
            saw_pro = saw_contra = False
            for clause in _clauses(p.content or ""):
                if not _mentions(clause, subject):
                    continue
                if _NEG_RE.search(clause):
                    saw_contra = True            # no break: a later clause may also speak
                elif _RECO_RE.search(clause):
                    saw_pro = True
            if saw_pro and not saw_contra:
                pro.append(p.role)
            elif saw_contra and not saw_pro:
                contra.append(p.role)
        if pro and contra:
            severity = "material" if subject in provided else "minor"  # inferred subj → secondary subject
            out.append(Conflict(
                kind="polarity", topic=subject, severity=severity, detector="polarity",
                sides=[ConflictSide(roles=pro, stance=f"pro {subject}"),
                       ConflictSide(roles=contra, stance=f"contra {subject}")],
            ))
    return out


def _numeric(ok, subjects, mode, spread, provided) -> List[Conflict]:
    groups: dict = {}  # (subject|None, unit) -> [(role, val)]
    for p in ok:
        for sent in _sentences(p.content or ""):
            for m in _NUM_RE.finditer(sent):
                val = _parse_num(m.group("num"))
                if val is None:
                    continue
                subj = _subject_for_number(sent, subjects, m.start()) if subjects else None
                groups.setdefault((subj, m.group("unit").lower()), []).append((p.role, val))
    out: List[Conflict] = []
    for (subj, unit), pairs in groups.items():
        vals = [v for _, v in pairs]
        if len(set(vals)) < 2:
            continue
        med = statistics.median(vals)
        if med == 0:
            continue
        rel = (max(vals) - min(vals)) / abs(med)
        if rel <= spread:
            continue
        hi = max(pairs, key=lambda rv: rv[1])
        lo = min(pairs, key=lambda rv: rv[1])
        out.append(Conflict(
            kind="numeric", topic=(f"{subj} {unit}" if subj else unit),
            severity=("blocking" if rel > 1.0 else "material"), detector="numeric",
            sides=[ConflictSide(roles=[hi[0]], stance=f"{hi[1]:g}{unit}"),
                   ConflictSide(roles=[lo[0]], stance=f"{lo[1]:g}{unit}")],
        ))
    return out


def _top_option(content: str, subjects: List[str]) -> Optional[str]:
    for sent in _sentences(content):
        if _RECO_RE.search(sent) and not _NEG_RE.search(sent):
            low = sent.lower()
            present = [(low.find(s.lower()), s) for s in subjects if _mentions(sent, s)]
            if present:
                return min(present)[1]            # earliest option IN the sentence (§3.2#3)
    counts = [(s, _count_mentions(content, s)) for s in subjects]
    counts = [(s, n) for s, n in counts if n > 0]
    return max(counts, key=lambda sn: sn[1])[0] if counts else None


def _recommendation(ok, subjects, mode, spread, provided) -> List[Conflict]:
    if mode not in (None, "decision", "comparison") or len(subjects) < 2:
        return []
    tops: dict = {}
    for p in ok:
        top = _top_option(p.content or "", subjects)
        if top is not None:
            tops.setdefault(top, []).append(p.role)
    if len(tops) < 2:
        return []
    return [Conflict(
        kind="recommendation", topic="top recommendation", severity="blocking",
        detector="recommendation",
        sides=[ConflictSide(roles=roles, stance=f"top: {opt}") for opt, roles in tops.items()],
    )]


def _negation_hits_core(sent: str, shared: set) -> bool:
    """True iff the negated token in *sent* belongs to the shared core (so the negation contradicts the
    common claim, not a peripheral word like "…nicht kritisch")."""
    words = [w.lower() for w in _WORD_RE.findall(sent or "")]
    for i, w in enumerate(words):
        if _NEG_RE.fullmatch(w):
            for nxt in words[i + 1:]:
                if len(nxt) >= 3 and nxt not in _STOP and not _NEG_RE.fullmatch(nxt):
                    return nxt in shared
    return False


def _claim(ok, subjects, mode, spread, provided) -> List[Conflict]:
    neg_units, pos_units = [], []
    for p in ok:
        for sent in _sentences(p.content or "")[:_MAX_SENTS]:
            toks = _content_tokens(sent)
            if len(toks) >= 2:
                (neg_units if _NEG_RE.search(sent) else pos_units).append((p.role, sent, toks))
    out: List[Conflict] = []
    for ri, si, ti in neg_units:                 # only negated × non-negated pairs (smaller than O(n²))
        for rj, sj, tj in pos_units:
            if ri == rj:
                continue
            shared = ti & tj
            if _jaccard(ti, tj) >= 0.6 and _negation_hits_core(si, shared):
                out.append(Conflict(
                    kind="claim", topic=" ".join(sorted(shared)[:4]), severity="material",
                    detector="claim",
                    sides=[ConflictSide(roles=[ri], stance=_clean_span(si), evidence=_clean_span(si)),
                           ConflictSide(roles=[rj], stance=_clean_span(sj), evidence=_clean_span(sj))],
                ))
    return out


# ── merge + sort ──────────────────────────────────────────────────────────────────────────────────
def _merge_sides(sides: List[ConflictSide]) -> List[ConflictSide]:
    by_stance: dict = {}
    for s in sides:
        if s.stance in by_stance:
            ex = by_stance[s.stance]
            ex.roles = sorted(set(ex.roles) | set(s.roles))
        else:
            by_stance[s.stance] = ConflictSide(roles=sorted(set(s.roles)), stance=s.stance,
                                               evidence=s.evidence)
    return list(by_stance.values())


def _merge_and_sort(conflicts: List[Conflict]) -> List[Conflict]:
    merged: dict = {}
    for c in conflicts:
        key = (c.kind, c.topic)
        if key in merged:
            ex = merged[key]
            ex.sides = _merge_sides(ex.sides + c.sides)
            if _SEV_RANK[c.severity] < _SEV_RANK[ex.severity]:
                ex.severity = c.severity
        else:
            merged[key] = c
    return sorted(merged.values(), key=lambda c: _SEV_RANK[c.severity])


def detect_conflicts(
    ok: Sequence[Any],
    *,
    subjects: Optional[List[str]] = None,
    mode: Optional[str] = None,
    numeric_spread: float = DEFAULT_NUMERIC_SPREAD,
    query: str = "",
) -> List[Conflict]:
    """Locate conflicts across the OK perspectives (deterministic, LLM-free, fail-soft).

    ``subjects=None`` → inferred from the contents (those yield ``minor`` polarity); when ``query`` is
    given the inference is anchored to it (only options actually named in the question, not every German
    noun — Live-Bug #4). Each sub-detector is isolated: a throwing one contributes []; the stage never
    raises and returns [] in the worst case. Conflicts with the same (kind, topic) merge (severity = max),
    sorted blocking-first.
    """
    explicit = subjects is not None
    subs = subjects if explicit else _infer_subjects(ok, query=query)
    provided = set(subs) if explicit else set()
    found: List[Conflict] = []
    # detectors resolved from module globals at CALL time (so a monkeypatched one is honoured, §8-B).
    for det in (_polarity, _numeric, _recommendation, _claim):
        try:
            found.extend(det(ok, subs, mode, numeric_spread, provided))
        except Exception:  # noqa: BLE001 — one sub-detector must not abort cross-verify
            continue
    return _merge_and_sort(found)
