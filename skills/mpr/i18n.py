"""MPR locale shim (#18-4c) — delegates to the shared core loader ``ack.i18n`` (#107).

The file-overlay loader now lives in core (`ack.i18n.Localizer`), so content i18n is available
independent of the MPR flag/plugin discovery. This module is a thin, back-compatible shim: it
binds one `Localizer` to `skills/mpr/locales` and re-exports the same module-level functions, so
every existing MPR call site (`i18n.t` / `role_lens` / `label` / `localized` / `use_language`)
is unchanged. English is the source/default; a `<lang>.json` overlay translates per dotted path.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ack.i18n import Localizer

#: Bound to MPR's own locales dir; the loader/mechanism is shared (ack.i18n), the data is MPR's.
_LOC = Localizer(Path(__file__).resolve().parent / "locales", en_labels={"question": "QUESTION"})


def use_language(lang: Optional[str]) -> None:
    _LOC.use_language(lang)


def role_lens(domain: str, role: str, default: str, lang: Optional[str]) -> str:
    return _LOC.role_lens(domain, role, default, lang)


def label(key: str, lang: Optional[str]) -> str:
    return _LOC.label(key, lang)


def localized(default: str, lang: Optional[str], *path: str) -> str:
    return _LOC.localized(default, lang, *path)


def t(default: str, *path: str) -> str:
    return _LOC.t(default, *path)
