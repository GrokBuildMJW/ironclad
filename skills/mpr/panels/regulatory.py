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
            "lens_prompt": "Du bist Regulierungs-Analyst für die EU. Bewerte die Frage strikt nach "
                           "geltendem und absehbarem EU-Recht (Verordnungen/Richtlinien, einschlägige "
                           "Behörden). Zitiere konkrete Rechtsakte/Artikel; markiere Unsicherheit "
                           "explizit.",
            "effort": "high",
        },
        {
            "role": "Jurisdiktion: US",
            "lens_prompt": "Du bist Regulierungs-Analyst für die USA (Bund + relevante Bundesstaaten). "
                           "Bewerte nach geltendem US-Recht/Regulator-Guidance; nenne Statute/"
                           "Agency-Rules; trenne Bundesebene von Staatenebene.",
            "effort": "high",
        },
        {
            "role": "Jurisdiktion: UAE",
            "lens_prompt": "Du bist Regulierungs-Analyst für die VAE (inkl. Freezones wie DIFC/ADGM). "
                           "Bewerte nach geltendem VAE-Recht und Freezone-Regimen; markiere, wo "
                           "Onshore vs. Freezone abweicht.",
            "effort": "high",
        },
        {
            "role": "Markt-Analyst",
            "lens_prompt": "Du bewertest die praktischen Markt-/Geschäftsfolgen der Regulierung: "
                           "Welche Geschäftsmodelle werden ermöglicht/blockiert, welche Marktdynamik "
                           "entsteht?",
            "effort": "medium",
        },
        {
            "role": "Compliance / Enforcement",
            "lens_prompt": "Du bewertest die Durchsetzungs-Realität: Wie aktiv wird vollzogen, welche "
                           "Strafen/Präzedenz-Bußen, was ist gelebte Praxis vs. Buchstabe?",
            "effort": "medium",
        },
        {
            "role": "Präzedenz / Case-Law",
            "lens_prompt": "Du bewertest einschlägige Rechtsprechung/Verwaltungs-Präzedenz: Welche "
                           "Fälle prägen die Auslegung, wohin tendiert die Linie?",
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
