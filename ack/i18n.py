"""Shared content i18n — a file-overlay locale loader (ADR-0002 #107).

Promoted from the MPR-local loader so the prompt library + any skill can localize **content**
(role prompts, labels, template strings) without depending on a flag-gated plugin. This is
**core** — always importable, independent of `GX10_MPR` / `GX10_PLUGINS_DIR`.

English is the source/default. A `<lang>.json` overlay in a caller-chosen **locales dir**
supplies translations along a dotted path; a missing language/key/file falls back to English,
so a partial or absent overlay can never break a run. Each caller passes its own locales dir
(mechanism shared, data per-domain). Zero external dependencies (stdlib only).

Distinct from `engine/messages.py`, which localizes the engine's own **chrome** (an in-code
catalog). This module is the content-overlay loader for skills/prompts.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


class Localizer:
    """A locale-overlay loader bound to one ``locales_dir`` (per-instance cache + active lang)."""

    def __init__(self, locales_dir: str | Path, *, en_labels: Optional[dict] = None) -> None:
        self._dir = Path(locales_dir)
        self._en_labels = dict(en_labels or {})
        self._active_lang = "en"
        self._cache: dict[str, dict] = {}

    def use_language(self, lang: Optional[str]) -> None:
        """Set the active render language read by :meth:`t` (callers set this once per request)."""
        self._active_lang = lang or "en"

    def _overlay(self, lang: str) -> dict:
        """Load + cache ``<locales_dir>/<lang>.json``. Missing/broken → ``{}`` (English fallback)."""
        if lang in self._cache:
            return self._cache[lang]
        p = self._dir / f"{lang}.json"
        data: dict = {}
        if p.is_file():
            try:
                loaded = json.loads(p.read_text(encoding="utf-8"))
                data = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                data = {}
        self._cache[lang] = data
        return data

    def role_lens(self, domain: str, role: str, default: str, lang: Optional[str]) -> str:
        """Localized lens prompt for (domain, role); English ``default`` fallback."""
        # I18N-1 (#503): walk via localized() so EACH nested level is isinstance-guarded — a malformed
        # overlay (e.g. "roles": "x") otherwise AttributeErrors on chained .get().get() and breaks the run.
        return self.localized(default, lang, "roles", domain, role)

    def label(self, key: str, lang: Optional[str]) -> str:
        """Localized UI label; English fallback (an absent/malformed overlay is harmless)."""
        if lang and lang != "en":
            labels = self._overlay(lang).get("labels")     # I18N-1 (#503): guard a non-dict "labels"
            if isinstance(labels, dict):
                v = labels.get(key)
                if isinstance(v, str) and v:
                    return v
        return self._en_labels.get(key, key)

    def localized(self, default: str, lang: Optional[str], *path: str) -> str:
        """Walk ``_overlay(lang)`` along ``*path``; return the leaf string if present+non-empty,
        else the English ``default``. English/absent → default."""
        if not lang or lang == "en":
            return default
        node: object = self._overlay(lang)
        for k in path:
            if not isinstance(node, dict):
                return default
            node = node.get(k)
        return node if isinstance(node, str) and node else default

    def t(self, default: str, *path: str) -> str:
        """Active-language localized lookup (uses :meth:`use_language`)."""
        return self.localized(default, self._active_lang, *path)
