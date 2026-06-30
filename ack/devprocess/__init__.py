"""``ack.devprocess`` — the curated, public dev-process surface (ADR-0011 AD-3).

The ONLY public Python under ``ack.devprocess`` is the versioned :mod:`ack.devprocess.api` facade. The
dev-process IMPLEMENTATION substrate is engine-internal and is **not** shipped in the wheel — the facade
reaches it through a registered driver (dependency inversion), so importing this package from the wheel
alone always succeeds and degrades cleanly when no engine is wired (see :mod:`ack.devprocess.api`).
"""
from __future__ import annotations

from .api import __version__

__all__ = ["__version__", "api"]
