"""Start-panel: regulatory (Spec 05 §7.2).

Mode ``evidence-research``, evidence ``external`` → roles inherit ``offloadable`` (public research may
go to the PC-CLI pool). ``synthesis_template = evidence-report``. The jurisdiction roles are a template:
the router instantiates one per relevant jurisdiction (adaptive, §7.4); the rest are fixed.
"""
from mpr.registry.schema import Panel

PANEL = Panel(
    domain="regulatory",
    mode="evidence-research",
    evidence_source="external",
    synthesis_template="evidence-report",
    effort_defaults={"default": "high"},
    description="Cross-jurisdiction regulatory analysis (EU/US/UAE) plus market, enforcement "
                "and precedent lenses.",
    roles=[
        {
            "role": "Jurisdiktion: EU",
            "lens_prompt": "You are a regulatory analyst for the EU. Judge the question strictly by "
                           "current and foreseeable EU law (regulations/directives, relevant agencies). "
                           "Cite concrete legal acts/articles; flag uncertainty explicitly.",
            "effort": "high",
        },
        {
            "role": "Jurisdiktion: US",
            "lens_prompt": "You are a regulatory analyst for the US (federal + relevant states). Judge by "
                           "current US law/regulator guidance; cite statutes/agency rules; separate the "
                           "federal level from the state level.",
            "effort": "high",
        },
        {
            "role": "Jurisdiktion: UAE",
            "lens_prompt": "You are a regulatory analyst for the UAE (incl. free zones like DIFC/ADGM). "
                           "Judge by current UAE law and free-zone regimes; flag where onshore vs. free "
                           "zone diverge.",
            "effort": "high",
        },
        {
            "role": "Markt-Analyst",
            "lens_prompt": "You judge the practical market/business consequences of the regulation: which "
                           "business models are enabled/blocked, what market dynamic emerges?",
            "effort": "medium",
        },
        {
            "role": "Compliance / Enforcement",
            "lens_prompt": "You judge enforcement reality: how actively is it enforced, what "
                           "penalties/precedent fines, what is lived practice vs. the letter of the law?",
            "effort": "medium",
        },
        {
            "role": "Präzedenz / Case-Law",
            "lens_prompt": "You judge relevant case law/administrative precedent: which cases shape the "
                           "interpretation, where is the line trending?",
            "effort": "high",
        },
    ],
)

CASE: dict = {
    "name": "mpr-panel-regulatory",
    "capability": "mpr.panel.regulatory",
    "domain": "regulatory",
    "description": PANEL.description,
}
# No run() — panels are data.
