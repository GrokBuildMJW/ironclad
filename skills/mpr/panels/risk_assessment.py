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
            "lens_prompt": "Du bewertest technische Risiken: Architektur-Schwächen, Skalierungs-/"
                           "Stabilitäts-Grenzen, technische Schulden, Single-Points-of-Failure. "
                           "Severity × Likelihood je Risiko.",
            "effort": "high",
            "provider_policy": "local-only",  # inspects internal code → never offloaded
        },
        {
            "role": "Operativ",
            "lens_prompt": "Du bewertest operative Risiken: Prozess-/Personal-/Lieferketten-/"
                           "Abhängigkeits-Ausfälle und Betriebskontinuität.",
            "effort": "medium",
        },
        {
            "role": "Regulatorisch",
            "lens_prompt": "Du bewertest regulatorische/Compliance-Risiken: Verstöße, Lizenz-/"
                           "Genehmigungs-Lücken, sich ändernde Vorgaben.",
            "effort": "medium",
        },
        {
            "role": "Finanziell",
            "lens_prompt": "Du bewertest finanzielle Risiken: Kosten-Runaway, Cashflow, Markt-/"
                           "Wechselkurs-/Konzentrations-Exposure.",
            "effort": "medium",
        },
        {
            "role": "Reputation",
            "lens_prompt": "Du bewertest Reputations-/Stakeholder-Risiken: öffentliche Wahrnehmung, "
                           "Vertrauensverlust, Eskalations-Pfade.",
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
