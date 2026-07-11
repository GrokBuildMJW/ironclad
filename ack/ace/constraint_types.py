"""Typed constraint fields + hard/soft classifier (epic #1344 S5 / #1341).

Pure, boundary-clean allow-list plumbing for the machine-checkable constraint subset
(``language``, ``network``). Normalization and provenance classification live here so
S3's detector, S6's hard-check, and the capture-completeness detector can share one
frozen contract without importing the engine.

No gate or design comparison lives here. This module normalizes, classifies, extracts,
and detects conservative DE/EN body signals for typed capture completeness. Default-off
engine flags keep the public surface byte-identical until callers wire them on. Never
raises on untrusted input (unknown → ``None`` / empty).
"""
from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional, Union

#: Allow-listed typed frontmatter / tool keys (frozen order = write order).
TYPED_KEYS: tuple[str, ...] = ("language", "network")

#: Provenance classes persisted as ``source:`` on the constraints document.
HARD = "hard"
SUGGESTED = "suggested"

#: Frozen language aliases → canonical token (lower-case).
_LANGUAGE_ALIASES: Mapping[str, str] = {
    "py": "python",
    "python": "python",
    "python3": "python",
    "rs": "rust",
    "rust": "rust",
    "js": "javascript",
    "node": "javascript",
    "javascript": "javascript",
    "ts": "typescript",
    "typescript": "typescript",
    "go": "go",
    "golang": "go",
}

#: Network-forbidden tokens → False (no network).
_NETWORK_FALSE: frozenset[str] = frozenset(
    {"none", "no", "off", "false", "0", "forbidden"}
)
#: Network-allowed tokens → True.
_NETWORK_TRUE: frozenset[str] = frozenset(
    {"allowed", "yes", "on", "true", "1"}
)

#: Explicit operator/author marker that elevates a capture to HARD.
_CONSTRAINTS_MARKER_RE = re.compile(r"(?im)^\s*Constraints\s*:")


def normalize_language(v: object) -> Optional[str]:
    """Lower-case + alias-fold a language token. Unknown / empty → ``None``. Never raises."""
    try:
        if v is None:
            return None
        if isinstance(v, bool):
            return None
        s = str(v).strip().lower()
        if not s:
            return None
        return _LANGUAGE_ALIASES.get(s)
    except Exception:  # noqa: BLE001 — pure: hostile __str__ never breaks a caller
        return None


def normalize_network(v: object) -> Optional[bool]:
    """Map a network token to ``True`` (allowed) / ``False`` (forbidden). Unknown → ``None``.

    Never raises. Bool inputs pass through; other types are string-folded.
    """
    try:
        if v is None:
            return None
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if v == 1:
                return True
            if v == 0:
                return False
            return None
        s = str(v).strip().lower()
        if not s:
            return None
        if s in _NETWORK_FALSE:
            return False
        if s in _NETWORK_TRUE:
            return True
        return None
    except Exception:  # noqa: BLE001
        return None


def has_constraints_marker(text: object) -> bool:
    """True when *text* carries an explicit ``Constraints:`` line. Never raises."""
    try:
        return bool(_CONSTRAINTS_MARKER_RE.search(str(text or "")))
    except Exception:  # noqa: BLE001
        return False


_LANG_TOKEN_RE = r"(python3|python|py|rust|rs|javascript|js|node|typescript|ts|golang|go)"
_LANG_OBJECT_NOUN_RE = r"(?!(?:\s+|[.-])\w*(?:mod|modules?|packages?|pakete?|paket|modul(?:e|en)?|path|registry|files?|datei(?:en)?|input|tests?|examples?|beispiel(?:e)?|library|libraries|bibliothek(?:en)?|stdlib|analy[sz]er|version|manager|toolchain|compiler|runtime|formatter|linter|sdk|binary|binaries|installer|verzeichnis|ordner))"
_LANG_QUALIFIER_RES: tuple[re.Pattern[str], ...] = (
    re.compile(rf"(?i)\b(?:implement(?:ed|s|ing)?|writ(?:e|es|ten|ing)|cod(?:e|ed|ing)|develop(?:ed|s|ing)?|build|building|built)\s+(?:it\s+|this\s+|everything\s+|the\s+\w+\s+)?(?:in|using|with)\s+{_LANG_TOKEN_RE}\b{_LANG_OBJECT_NOUN_RE}"),
    re.compile(rf"(?i)\b(?:written\s+in|geschrieben\s+in)\s+{_LANG_TOKEN_RE}\b{_LANG_OBJECT_NOUN_RE}"),
    re.compile(rf"(?i)\b(?:language|sprache)\s*:\s*{_LANG_TOKEN_RE}\b"),
    re.compile(rf"(?i)\bprogrammiersprache:?\s*{_LANG_TOKEN_RE}\b"),
    re.compile(rf"(?i)\b(?:verwende|nutze)\s+{_LANG_TOKEN_RE}\b{_LANG_OBJECT_NOUN_RE}"),
    re.compile(rf"(?i)\b(?:use)\s+{_LANG_TOKEN_RE}\b{_LANG_OBJECT_NOUN_RE}"),
    re.compile(rf"(?i)\b{_LANG_TOKEN_RE}\s+only\b"),
    re.compile(rf"(?i)\bnur\s+{_LANG_TOKEN_RE}\b"),
    re.compile(rf"(?i)\bmust\s+be\s+{_LANG_TOKEN_RE}\b"),
    re.compile(rf"(?i)\bmuss\s+{_LANG_TOKEN_RE}\s+sein\b"),
    re.compile(rf"(?i)\brequires\s+{_LANG_TOKEN_RE}\b"),
)
_LANG_DOCS_CONTEXT_RE = re.compile(
    rf"(?i)\b(?:document|docs?|documentation|example|examples|test|tests)\s+{_LANG_TOKEN_RE}[-\s]+only\b"
)
_NETWORK_SIGNAL_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bno\s+(?:\w+\s+){0,1}(?:network|internet)\w*"),
    re.compile(r"(?i)\bno\s+network\b"),
    re.compile(r"(?i)\bno\s+internet(?:\s+access)?\b"),
    re.compile(r"(?i)\bno\s+online\b"),
    re.compile(r"(?i)\boffline[\s-]+only\b"),
    re.compile(r"(?i)\bmust\s+(?:run\s+|be\s+|stay\s+)?offline\b"),
    re.compile(r"(?i)\bruns?\s+offline\b"),
    re.compile(r"(?i)\bnetwork\s+(?:is\s+)?forbidden\b"),
    re.compile(r"(?i)\bwithout\s+(?:\w+\s+){0,1}(?:network|internet)\w*"),
    re.compile(r"(?i)\bnetwork\s+access\s+(?:is\s+)?(?:not\s+allowed|forbidden|denied|disabled|not\s+permitted)\b"),
    re.compile(r"(?i)\bnetwork\s*[:=]\s*(?:none|false|off|forbidden|disabled|blocked)\b"),
    re.compile(r"(?i)\bmuss\s+offline\b"),
    re.compile(r"(?i)\bkein(?:e|er|en|em|es|erlei)?\b(?:\s+\w+){0,1}\s+netzwerk\w*"),
    re.compile(r"(?i)\bkein(?:e|er|en|em|es|erlei)?\b(?:\s+\w+){0,1}\s+internet\w*"),
    re.compile(r"(?i)\bohne\s+(?:\w+\s+){0,1}(?:netzwerk\w*|internet\w*)\b"),
    re.compile(r"(?i)\bnetzwerk\w*\s+verboten\b"),
    re.compile(r"(?i)\binternet\w*\s+verboten\b"),
    re.compile(r"(?i)\bohne\s+netz\b"),
)


def body_states_typed_constraint(text: object) -> frozenset[str]:
    """Detect typed categories strongly stated in constraint prose.

    The detector is intentionally conservative: it only emits the category names
    whose typed fields should be present, and language mentions need an
    implementation/constraint qualifier rather than bare adjacency.
    """
    cats: set[str] = set()
    try:
        body = str(text or "")
        if not body:
            return frozenset()
        for pat in _LANG_QUALIFIER_RES:
            m = pat.search(body)
            if not m:
                continue
            start = max(0, m.start() - 32)
            end = min(len(body), m.end() + 32)
            if _LANG_DOCS_CONTEXT_RE.search(body[start:end]):
                continue
            lang = normalize_language(m.group(1))
            if lang is not None:
                cats.add("language")
                break
        if any(pat.search(body) for pat in _NETWORK_SIGNAL_RES):
            cats.add("network")
        return frozenset(cats)
    except Exception:  # noqa: BLE001
        return frozenset()


def classify(
    *,
    explicit_marker: bool = False,
    typed_supplied: bool = False,
    source: str = "",
) -> str:
    """Return ``HARD`` or ``SUGGESTED`` provenance.

    - **HARD** when an explicit ``Constraints:`` marker is present, or a typed param
      was supplied without a model-heuristic source.
    - **SUGGESTED** when the capture is a model heuristic only
      (``source='suggested'`` and no explicit marker). Typed fields may still be
      recorded under ``source: suggested``; the engine makes those values
      engine-visible via a complementary reader (``_constraint_typed_unresolved``);
      advisory until the design-approval softcheck / steering consume it (S2/S3).

    Pure and never raises.
    """
    try:
        src = (source or "").strip().lower()
        if explicit_marker:
            return HARD
        if src == "suggested":
            return SUGGESTED
        if typed_supplied:
            return HARD
        if src == HARD:
            return HARD
        # Heuristic-only path with no typed floor and no explicit source → suggested
        # is reserved for the model; a plain capture without signals is HARD when a
        # caller still asks for a class (body-only operator capture).
        return HARD
    except Exception:  # noqa: BLE001
        return HARD


def _frontmatter_kv(text: str) -> Dict[str, str]:
    """Minimal flat ``key: value`` extraction between ``---`` fences. Never raises."""
    try:
        lines = (text or "").splitlines()
        if not lines or lines[0].strip() != "---":
            # Fall through: still scan free-form ``language:`` / ``network:`` lines.
            return _scan_typed_lines(text)
        out: Dict[str, str] = {}
        for s in lines[1:]:
            if s.strip() == "---":
                break
            if ":" in s and not s.lstrip().startswith("#"):
                k, _, v = s.partition(":")
                k = k.strip().lower()
                if k:
                    out[k] = v.strip()
        # Also accept free-form body keys (e.g. under a Constraints: block).
        body_start = None
        for i, s in enumerate(lines[1:], 1):
            if s.strip() == "---":
                body_start = i + 1
                break
        if body_start is not None:
            for k, v in _scan_typed_lines("\n".join(lines[body_start:])).items():
                out.setdefault(k, v)
        return out
    except Exception:  # noqa: BLE001
        return {}


def _scan_typed_lines(text: str) -> Dict[str, str]:
    """Pull bare ``language:`` / ``network:`` lines from free text. Never raises."""
    out: Dict[str, str] = {}
    try:
        for line in (text or "").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or ":" not in s:
                continue
            k, _, v = s.partition(":")
            k = k.strip().lower()
            if k in TYPED_KEYS and k not in out:
                out[k] = v.strip()
    except Exception:  # noqa: BLE001
        return out
    return out


def parse_typed(text_or_frontmatter: Union[str, Mapping[str, Any], None]) -> Dict[str, Any]:
    """Extract allow-listed typed keys with normalized values.

    Accepts a frontmatter mapping or a markdown / free-text document. Returns only
    keys that normalize successfully (language → str, network → bool). Deterministic,
    pure, never raises — unknown / invalid → omitted.
    """
    try:
        if text_or_frontmatter is None:
            return {}
        if isinstance(text_or_frontmatter, Mapping):
            raw = {str(k).strip().lower(): text_or_frontmatter[k] for k in text_or_frontmatter}
        else:
            raw = _frontmatter_kv(str(text_or_frontmatter))
        out: Dict[str, Any] = {}
        if "language" in raw:
            lang = normalize_language(raw.get("language"))
            if lang is not None:
                out["language"] = lang
        if "network" in raw:
            net = normalize_network(raw.get("network"))
            if net is not None:
                out["network"] = net
        return out
    except Exception:  # noqa: BLE001
        return {}
