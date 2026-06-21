"""MPR core built-in entry — the ONE file ironclad's discover_skills scans (CASE + run).

MPR is now a **core built-in** (ADR-0002 #115): always loaded from `skills/`, no
`GX10_MPR` load gate. It is **always** registered as the `mpr_research` tool; the live
on/off is the **runtime** config `mpr.enabled` (default ON) — when off, the tool returns a
short "disabled" note instead of running (see `mpr.entry`). Thin by contract: all logic lives
in `mpr.entry`. (Bootstraps `skills/` onto sys.path so the standalone loader can import
the `mpr` package absolutely.)
"""
import sys
from pathlib import Path

_SKILLS = Path(__file__).resolve().parents[2]   # skills/  → import mpr.*
if str(_SKILLS) not in sys.path:
    sys.path.insert(0, str(_SKILLS))

from mpr.entry import build_case, mpr_research_run  # noqa: E402

CASE = build_case()
run = mpr_research_run
