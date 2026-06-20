"""MPR plugin entry — the ONE file ironclad's discover_skills scans (CASE + run, Spec 10 §2/§3/§5).

Loaded standalone by the loader (spec_from_file_location, registry.py:389) → no package context → it
bootstraps ``skills/`` onto sys.path and imports the ``mpr`` package absolutely. Thin by contract: all
logic lives in ``mpr.entry``. Flag-gated (§5): when GX10_MPR is off, this module exports NO CASE/run, so
``discover_skills`` registers no tool → the turn is byte-identical to "no plugin" (A/B off).
"""
import sys
from pathlib import Path

_SKILLS = Path(__file__).resolve().parents[2]   # skills/  → import mpr.*
if str(_SKILLS) not in sys.path:
    sys.path.insert(0, str(_SKILLS))

from mpr.entry import build_case, mpr_enabled, mpr_research_run  # noqa: E402

if mpr_enabled():
    CASE = build_case()
    run = mpr_research_run
# else: no CASE / no run → _registration_from_skill_module returns None → no tool (byte-identical off).
