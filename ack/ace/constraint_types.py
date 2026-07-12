"""Typed design/framing metadata normalization.

Pure, boundary-clean allow-list plumbing for the machine-checkable metadata subset
(``language``, ``network``). S1 retired the product constraint HARD-floor readers,
but design metadata still uses these normalizers and ``parse_typed``. Never raises on
untrusted input (unknown -> ``None`` / empty).
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Union

#: Allow-listed typed frontmatter / tool keys (frozen order = write order).
TYPED_KEYS: tuple[str, ...] = ("language", "network")

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
