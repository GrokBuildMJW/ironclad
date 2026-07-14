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
        # self-maintaining vault — typed-edge graph + lifecycle + reconcile/initiative chrome (#601 S12e)
        "vault.lifecycle_auto": "Auto-generated (reconcile_vault, LLM-free) — {nodes} node(s), {edges} edge(s).",
        "vault.related_suffix": ", {n} related block(s) updated",
        "vault.index_only_suffix": " (index only)",
        "vault.no_initiative": "no initiative '{slug}' under {root}/",
        "init.unknown_type": "unknown initiative type '{type}' — allowed: {allowed}",
        "init.needs_name": "an initiative needs a name",
        "init.unknown_slug": "no initiative '{slug}' under {root}/ — run `/project new <name>` first",
        "init.meta_body": "Initiative (type: {type}). Artifacts under `{path}/`. INDEX.md is maintained automatically (reconcile).",
        # /initiative command chrome
        "init.no_active": "no active initiative — run `/project new <name>` (or `/project use <slug>`) first",
        "init.cmd_created": "[initiative] created + active: {slug} (type {type}) -> {path}/\n  artifacts ({visible}) land here now; INDEX.md is maintained automatically.",
        "init.cmd_active": "[initiative] active: {slug} (type {type}) -> {path}/",
        "init.cmd_none": "[initiative] none — `/project new <name>`",
        "init.cmd_none_active": "[initiative] none active — `/project new …` or `/project use <slug>`",
        "init.cmd_reconcile_needs_slug": "[initiative] reconcile: no initiative given/active",
        # #938: command-ergonomics chrome (epic #927) — the new user-facing ENGINE outputs
        "confirm.destructive": "irreversible — this can delete work; nothing changed. Re-run with --yes to confirm.",
        "ace.warmup_done": "ace warmup: replayed {samples} trajectory record(s) into the playbook — +{added} bullet(s), {pruned} pruned",
        "ace.eval_j1_pass": "never rewrote the whole playbook ✓",
        "ace.eval_j1_fail": "rewrote the whole playbook ✗",
        "ace.eval_j2_over": "✓ over the 50% target",
        "ace.eval_j2_under": "✗ under the 50% target",
        "ace.eval_verdict": "ace eval: ACE learned from {n} past run(s) using {calls} model call(s) and {j1clause} (J-001 no-full-rewrite: {j1}). That is {reduction} fewer model calls than the evolutionary baseline ({j2clause}; J-002: {j2}). [calls — ACE {ace} · full-rewrite {fr} · evolutionary {evo}]",
        # #956: the remaining new command-ergonomics engine chrome (EN source)
        "keys.header": "Config keys ({n}) — /config get <key> · /config set <key> <value>:",
        "keys.boot_only": "[boot-only]",
        "tiers.header": "Commands by danger tier (command-spec):",
        "tiers.read_only": "read-only",
        "tiers.mutating": "mutating (change state)",
        "tiers.costly": "costly (spend model turns / spawn work)",
        "tiers.destructive": "destructive (can delete work)",
        "config.unknown_key": "[config] refused: unknown key '{name}' — its root section is not in the config (typo?). See /config keys. Nothing was written.",
        "skills.params": "params:",
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
        "vault.lifecycle_auto": "Automatisch generiert (reconcile_vault, LLM-frei) — {nodes} Knoten, {edges} Kante(n).",
        "vault.related_suffix": ", {n} Related-Block/Blöcke aktualisiert",
        "vault.index_only_suffix": " (nur Index)",
        "vault.no_initiative": "kein Initiative '{slug}' unter {root}/",
        "init.unknown_type": "unbekannter Initiative-Typ '{type}' — erlaubt: {allowed}",
        "init.needs_name": "Initiative braucht einen Namen",
        "init.unknown_slug": "kein Initiative '{slug}' unter {root}/ — `/project new <name>` zuerst",
        "init.meta_body": "Initiative (type: {type}). Artefakte unter `{path}/`. INDEX.md wird automatisch gepflegt (reconcile).",
        "init.no_active": "kein aktives Initiative — `/project new <name>` (oder `/project use <slug>`) zuerst",
        "init.cmd_created": "[initiative] angelegt + active: {slug} (type {type}) -> {path}/\n  Artefakte ({visible}) landen jetzt hier; INDEX.md wird automatisch gepflegt.",
        "init.cmd_active": "[initiative] active: {slug} (type {type}) -> {path}/",
        "init.cmd_none": "[initiative] keine — `/project new <name>`",
        "init.cmd_none_active": "[initiative] keins active — `/project new …` oder `/project use <slug>`",
        "init.cmd_reconcile_needs_slug": "[initiative] reconcile: kein Initiative angegeben/active",
        # #938: command-ergonomics chrome (epic #927)
        "confirm.destructive": "unumkehrbar — dies kann Arbeit löschen; nichts geändert. Zur Bestätigung erneut mit --yes ausführen.",
        "ace.warmup_done": "ace warmup: {samples} Trajektorien-Datensatz/-sätze ins Playbook eingespielt — +{added} Bullet(s), {pruned} entfernt",
        "ace.eval_j1_pass": "hat das ganze Playbook nie neu geschrieben ✓",
        "ace.eval_j1_fail": "hat das ganze Playbook neu geschrieben ✗",
        "ace.eval_j2_over": "✓ über dem 50%-Ziel",
        "ace.eval_j2_under": "✗ unter dem 50%-Ziel",
        "ace.eval_verdict": "ace eval: ACE lernte aus {n} früheren Lauf/Läufen mit {calls} Modellaufruf(en) und {j1clause} (J-001 no-full-rewrite: {j1}). Das sind {reduction} weniger Modellaufrufe als die evolutionäre Baseline ({j2clause}; J-002: {j2}). [Aufrufe — ACE {ace} · full-rewrite {fr} · evolutionär {evo}]",
        # #956: the same keys, DE overlay
        "keys.header": "Config-Keys ({n}) — /config get <key> · /config set <key> <value>:",
        "keys.boot_only": "[boot-only]",
        "tiers.header": "Befehle nach Gefahren-Stufe (command-spec):",
        "tiers.read_only": "nur lesen",
        "tiers.mutating": "verändernd (Zustand)",
        "tiers.costly": "kostspielig (Modell-Turns / erzeugt Arbeit)",
        "tiers.destructive": "destruktiv (kann Arbeit löschen)",
        "config.unknown_key": "[config] abgelehnt: unbekannter Key '{name}' — die Root-Sektion ist nicht in der Config (Tippfehler?). Siehe /config keys. Nichts geschrieben.",
        "skills.params": "Parameter:",
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
