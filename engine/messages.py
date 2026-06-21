"""Engine UI/chrome message catalog (i18n) — #18-4a.

The model's *reply* language is handled separately (``_language_guidance`` injects a per-turn
directive). This catalog localizes the engine's OWN user-facing chrome — status labels, error
texts, and the prose written into the self-maintaining vault (INDEX/related headings).

- Keys are stable, English, dotted (e.g. ``status.ready``). Call sites use ``msg("status.ready")``.
- English is the SOURCE/default language; any locale falls back to English per key, and an unknown
  key returns ``[key]`` (loud but non-fatal).
- The active language is read live from ``gx10.LANGUAGE`` (config ``generation.language`` /
  ``GX10_LANGUAGE``); pass ``lang=`` to override (e.g. when rendering vault files for a fixed locale).
"""
from __future__ import annotations

from typing import Optional

#: language code -> {dotted key -> template}. Add a locale by adding a sub-dict; missing keys
#: fall back to English. Keep keys English; only the values are translated.
_MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        # status bar / streaming chrome
        "status.ready": "ready",
        "status.messages": "Messages",
        "status.chars": "Characters",
        "status.last_gen": "Last gen",
        "status.streaming_engine": "Orchestrator Engine  ·  streaming  |  exit = quit",
        "hint.cancel": "Ctrl+C = cancel",
        "hint.thinking_cancel": "{model} thinking… {secs}s     Esc = cancel",
        "hint.input": "Ask something · /help · exit",
        "hint.repl_help": "/help · exit · Esc = cancel · Ctrl+C = copy last answer",
        "label.selection": "Selection ({n} chars)",
        # errors
        "error.no_last_answer": "[ERROR] No previous answer!",
        # self-maintaining vault prose (written INTO vault files; localized to the active language)
        "vault.index_auto": "Auto-maintained (reconcile_vault, LLM-free) — {n} document(s).",
        "vault.related_heading": "Related (auto)",
        "vault.indexed": "{slug}: {n} document(s) indexed{suffix}",
    },
    "de": {
        "status.ready": "bereit",
        "status.messages": "Nachrichten",
        "status.chars": "Zeichen",
        "status.last_gen": "Letzte Gen",
        "status.streaming_engine": "Orchestrator Engine  ·  streaming  |  exit = Beenden",
        "hint.cancel": "Strg+C = abbrechen",
        "hint.thinking_cancel": "{model} denkt… {secs}s     Esc = abbrechen",
        "hint.input": "Frag etwas · /help · exit",
        "hint.repl_help": "/help · exit · Esc = abbrechen · Strg+C = letzte Antwort kopieren",
        "label.selection": "Auswahl ({n} Zeichen)",
        "error.no_last_answer": "[FEHLER] Keine letzte Antwort!",
        "vault.index_auto": "Automatisch gepflegt (reconcile_vault, LLM-frei) — {n} Dokument(e).",
        "vault.related_heading": "Verwandt (auto)",
        "vault.indexed": "{slug}: {n} Dokument(e) indiziert{suffix}",
    },
}


def msg(key: str, lang: Optional[str] = None, **fmt: object) -> str:
    """Return the localized chrome string for *key*. Falls back English→``[key]``; formats with **fmt."""
    if lang is None:
        try:                                  # live language, late import to avoid an import cycle
            import gx10  # type: ignore
            lang = getattr(gx10, "LANGUAGE", "en")
        except Exception:
            lang = "en"
    code = (lang or "en").lower()
    table = _MESSAGES.get(code) or _MESSAGES["en"]
    text = table.get(key) or _MESSAGES["en"].get(key) or f"[{key}]"
    try:
        return text.format(**fmt) if fmt else text
    except (KeyError, IndexError, ValueError):
        return text
