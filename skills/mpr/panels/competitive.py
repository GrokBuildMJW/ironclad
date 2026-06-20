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
            "lens_prompt": "Du bist Produkt-Analyst. Vergleiche allein Funktionsumfang, UX und "
                           "Roadmap-Signale der Wettbewerber gegen die Frage; nenne klare "
                           "Differenzierer und Paritäts-Punkte.",
            "effort": "medium",
        },
        {
            "role": "Pricing / GTM",
            "lens_prompt": "Du bist Go-to-Market-Analyst. Vergleiche Preismodelle, Packaging, "
                           "Vertriebsmotion und Zielsegment; wo gewinnt/verliert man auf der "
                           "Kommerz-Achse?",
            "effort": "medium",
        },
        {
            "role": "Tech-Moat",
            "lens_prompt": "Du bist Tech-Stratege. Bewerte die Verteidigbarkeit: technologische "
                           "Burggräben, Daten-/Netzwerk-Effekte, Wechselkosten. Wie nachhaltig ist "
                           "der Vorsprung?",
            "effort": "high",
        },
        {
            "role": "Kunde / Use-Case",
            "lens_prompt": "Du nimmst die Kundensicht ein. Vergleiche je Kern-Use-Case/Segment: "
                           "wessen Lösung passt für wen besser und warum (Jobs-to-be-done)?",
            "effort": "medium",
        },
        {
            "role": "Risiko / Bedrohung",
            "lens_prompt": "Du bist Wettbewerbs-Aufklärer. Identifiziere die größten Bedrohungen: "
                           "aufstrebende Spieler, Substitution, strategische Züge der Etablierten.",
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
