"""Start-panel: competitive (Spec 05 §7.3).

Mode ``comparison``, evidence ``external`` → roles inherit ``offloadable``.
``synthesis_template = comparison-matrix``.
"""
from mpr.registry.schema import Panel

PANEL = Panel(
    domain="competitive",
    mode="comparison",
    evidence_source="external",
    synthesis_template="comparison-matrix",
    effort_defaults={"default": "medium"},
    description="Competitive comparison across product, pricing/GTM, tech-moat, customer "
                "and threat lenses.",
    roles=[
        {
            "role": "Produkt",
            "lens_prompt": "You are a product analyst. Compare solely feature scope, UX and roadmap "
                           "signals of the competitors against the question; name clear differentiators "
                           "and parity points.",
            "effort": "medium",
        },
        {
            "role": "Pricing / GTM",
            "lens_prompt": "You are a go-to-market analyst. Compare pricing models, packaging, sales "
                           "motion and target segment; where do you win/lose on the commercial axis?",
            "effort": "medium",
        },
        {
            "role": "Tech-Moat",
            "lens_prompt": "You are a tech strategist. Judge defensibility: technological moats, "
                           "data/network effects, switching costs. How sustainable is the lead?",
            "effort": "high",
        },
        {
            "role": "Kunde / Use-Case",
            "lens_prompt": "You take the customer's view. Compare per core use-case/segment: whose "
                           "solution fits whom better and why (jobs-to-be-done)?",
            "effort": "medium",
        },
        {
            "role": "Risiko / Bedrohung",
            "lens_prompt": "You are a competitive-intelligence scout. Identify the biggest threats: "
                           "emerging players, substitution, strategic moves by incumbents.",
            "effort": "medium",
        },
    ],
)

CASE: dict = {
    "name": "mpr-panel-competitive",
    "capability": "mpr.panel.competitive",
    "domain": "competitive",
    "description": PANEL.description,
}
# No run() — panels are data.
