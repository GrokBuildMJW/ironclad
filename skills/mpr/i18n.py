"""MPR locale loader (#18-4c) — role lens_prompts + UI labels decoupled into data files.

English is the source/default: the panels carry the canonical English ``lens_prompt`` and the labels
below default to English. A ``<lang>.json`` overlay beside this module (e.g. ``locales/de.json``)
supplies translations per ``roles[domain][role]`` / ``labels[key]``. A missing language/key falls back
to English — so a partial or absent overlay can never break a run. Adding a language = dropping a data
file (no code change), which is the whole point: roles are decoupled from code and multilingual.
"""
from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Optional

_LOCALES_DIR = Path(__file__).resolve().parent / "locales"

#: English source labels (the panels hold the English lens_prompts). Overlays may translate these.
_EN_LABELS = {"question": "QUESTION"}

#: Render-language context for the deterministic template renderers (#44). They sit deep in the
#: validate_template→validate_*→render_* chain; threading a ``lang`` param through every signature
#: would churn the whole template API + its tests. Instead synthesize() sets this once before
#: rendering (language is deployment-fixed via GX10_LANGUAGE and synthesis runs sequentially per
#: request), and the renderers read it via ``localized(default, ...)`` with no explicit lang.
_ACTIVE_LANG = "en"


def use_language(lang: Optional[str]) -> None:
    """Set the render-language context read by the template renderers (synthesize() calls this)."""
    global _ACTIVE_LANG
    _ACTIVE_LANG = (lang or "en")


@functools.lru_cache(maxsize=16)
def _overlay(lang: str) -> dict:
    """Load ``locales/<lang>.json`` once (cached). Missing/broken file → ``{}`` (English fallback)."""
    p = _LOCALES_DIR / f"{lang}.json"
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def role_lens(domain: str, role: str, default: str, lang: Optional[str]) -> str:
    """Localized lens_prompt for (domain, role); falls back to the English ``default`` (panel source)."""
    if not lang or lang == "en":
        return default
    return (_overlay(lang).get("roles", {}).get(domain, {}).get(role)) or default


def label(key: str, lang: Optional[str]) -> str:
    """Localized UI label; English fallback (so an absent overlay is harmless)."""
    if lang and lang != "en":
        v = _overlay(lang).get("labels", {}).get(key)
        if v:
            return v
    return _EN_LABELS.get(key, key)


def localized(default: str, lang: Optional[str], *path: str) -> str:
    """Generic overlay lookup: walk ``_overlay(lang)`` along ``*path`` and return the leaf string if
    present+non-empty, else the English ``default`` (the source string in code). English/absent → default.
    Used for the synthesis prompt layer (synthesis.system / .mode_extra[mode] / .labels[key] / messages)."""
    if not lang or lang == "en":
        return default
    node: object = _overlay(lang)
    for k in path:
        if not isinstance(node, dict):
            return default
        node = node.get(k)
    return node if isinstance(node, str) and node else default


def t(default: str, *path: str) -> str:
    """Render-context localized lookup (#44): uses the active render language (``use_language``).
    English ``default`` is the source/fallback. Lets the template renderers localize without a lang
    param threaded through the validate_template→render_* chain."""
    return localized(default, _ACTIVE_LANG, *path)
