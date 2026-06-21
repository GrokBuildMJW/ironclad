"""Start-panel: risk-assessment (Spec 05 §7.4).

Mode ``evidence-research``, evidence ``mixed`` → default ``offloadable``, BUT the technical role is
explicitly ``local-only`` (it often inspects internal code/architecture — an example of a per-role
override against the evidence_source default). ``synthesis_template = risk-register``.

NOTE the slug: ``risk-assessment`` is binding (Spec 05 §7.4 warning — Spec 02:92's ``risk`` is a typo;
``resolve("risk")`` would miss this declared panel and fall to adhoc).
"""
from mpr.registry.schema import Panel

PANEL = Panel(
    domain="risk-assessment",
    mode="evidence-research",
    evidence_source="mixed",
    synthesis_template="risk-register",
    effort_defaults={"default": "medium"},
    description="Risk register across technical (local-only), operational, regulatory, financial "
                "and reputation lenses.",
    roles=[
        {
            "role": "Technisch",
            "lens_prompt": "You judge technical risks: architecture weaknesses, scaling/stability "
                           "limits, technical debt, single points of failure. Severity × likelihood "
                           "per risk.",
            "effort": "high",
            "provider_policy": "local-only",  # inspects internal code → never offloaded
        },
        {
            "role": "Operativ",
            "lens_prompt": "You judge operational risks: process/staffing/supply-chain/dependency "
                           "failures and business continuity.",
            "effort": "medium",
        },
        {
            "role": "Regulatorisch",
            "lens_prompt": "You judge regulatory/compliance risks: violations, licensing/permit gaps, "
                           "changing requirements.",
            "effort": "medium",
        },
        {
            "role": "Finanziell",
            "lens_prompt": "You judge financial risks: cost runaway, cashflow, market/exchange-rate/"
                           "concentration exposure.",
            "effort": "medium",
        },
        {
            "role": "Reputation",
            "lens_prompt": "You judge reputation/stakeholder risks: public perception, loss of trust, "
                           "escalation paths.",
            "effort": "low",
        },
    ],
)

CASE: dict = {
    "name": "mpr-panel-risk-assessment",
    "capability": "mpr.panel.risk-assessment",
    "domain": "risk-assessment",
    "description": PANEL.description,
}
# No run() — panels are data.
