"""Start-panel: architecture-decision (Spec 05 §7.1).

Mode ``decision``, evidence ``internal`` → every role resolves to ``local-only`` (private code stays
Spark-local). ``synthesis_template = decision-matrix``. Loaded standalone by ``PanelRegistry.discover``
(spec_from_file_location), so the import is absolute (``skills/`` is on sys.path at load time).
"""
from mpr.registry.schema import Panel

PANEL = Panel(
    domain="architecture-decision",
    mode="decision",
    evidence_source="internal",
    synthesis_template="decision-matrix",
    effort_defaults={"default": "high"},
    description="Multi-lens evaluation of a software architecture decision "
                "(maintainability, ops, security, performance, reversibility, team-fit, cost).",
    roles=[
        {
            "role": "Maintainer / Evolvability",
            "lens_prompt": "Du bist der Langzeit-Maintainer dieses Codebestands. Bewerte die Frage "
                           "allein nach Wart-, Erweiter- und Verständlichkeit über Jahre: Wie altert "
                           "dieser Ansatz, wie schwer ist eine spätere Änderung, welche Schulden "
                           "entstehen? Nenne konkrete Wartungs-Risiken am realen Repo-Stand.",
            "effort": "high",
        },
        {
            "role": "SRE / Ops",
            "lens_prompt": "Du bist Site-Reliability-Engineer im Bereitschaftsdienst. Bewerte "
                           "ausschließlich Betreib-/Observier-/Recover-barkeit: Deployment, Rollback, "
                           "Failure-Modes, Monitoring, Toil. Was bricht um 3 Uhr nachts und wie "
                           "schnell ist es erkenn-/behebbar?",
            "effort": "high",
        },
        {
            "role": "Security-Architekt",
            "lens_prompt": "Du bist Security-Architekt mit Zero-Trust-Mandat. Bewerte ausschließlich "
                           "die Sicherheits-/Trust-Boundary-Folgen: Angriffsfläche, Secrets/Tenancy, "
                           "Berechtigungen, Datenfluss-Souveränität. Wo öffnet diese Entscheidung "
                           "eine Lücke?",
            "effort": "high",
        },
        {
            "role": "Performance / Scale",
            "lens_prompt": "Du bist Performance-Engineer. Bewerte ausschließlich Durchsatz, Latenz, "
                           "Ressourcen-Profil und Skalierungspfad (inkl. Hardware-Limits wie "
                           "Bandbreite). Wo ist der Engpass, wie verhält es sich unter Last?",
            "effort": "high",
        },
        {
            "role": "Reversibilität / Lock-in",
            "lens_prompt": "Du bist auf Optionswert spezialisiert. Bewerte allein, wie reversibel "
                           "diese Entscheidung ist: Lock-in (Vendor/Format/API), Exit-Kosten, "
                           "Einbahn- vs. Zweibahn-Tür. Wie teuer wäre das Zurückrudern in 12 Monaten?",
            "effort": "medium",
        },
        {
            "role": "Team-Fit",
            "lens_prompt": "Du bewertest die Passung zu Team-Skills, vorhandenen Konventionen und "
                           "Lernkurve. Kann das Team das nachhaltig betreiben/erweitern, oder erzeugt "
                           "es eine Wissens-Insel?",
            "effort": "medium",
        },
        {
            "role": "Kosten / TCO",
            "lens_prompt": "Du bewertest Total-Cost-of-Ownership: Build-, Betriebs-, Lizenz-, Compute- "
                           "und Opportunitätskosten über den Lebenszyklus — nicht nur die Anschaffung.",
            "effort": "medium",
        },
    ],
)

CASE: dict = {
    "name": "mpr-panel-architecture-decision",
    "capability": "mpr.panel.architecture-decision",
    "domain": "architecture-decision",
    "description": PANEL.description,
}
# No run() — panels are data, not an executable skill. The only run() is in skills/mpr/skills/mpr_research.py.
