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
            "lens_prompt": "You are the long-term maintainer of this codebase. Judge the question solely "
                           "by maintainability, extensibility and comprehensibility over years: how does "
                           "this approach age, how hard is a later change, what debt accrues? Name "
                           "concrete maintenance risks against the real repo state.",
            "effort": "high",
        },
        {
            "role": "SRE / Ops",
            "lens_prompt": "You are a site-reliability engineer on call. Judge exclusively operability, "
                           "observability and recoverability: deployment, rollback, failure modes, "
                           "monitoring, toil. What breaks at 3am and how fast is it detectable/fixable?",
            "effort": "high",
        },
        {
            "role": "Security Architect",
            "lens_prompt": "You are a security architect with a zero-trust mandate. Judge exclusively the "
                           "security/trust-boundary consequences: attack surface, secrets/tenancy, "
                           "permissions, data-flow sovereignty. Where does this decision open a gap?",
            "effort": "high",
        },
        {
            "role": "Performance / Scale",
            "lens_prompt": "You are a performance engineer. Judge exclusively throughput, latency, "
                           "resource profile and scaling path (including hardware limits like bandwidth). "
                           "Where is the bottleneck, how does it behave under load?",
            "effort": "high",
        },
        {
            "role": "Reversibility / Lock-in",
            "lens_prompt": "You specialise in option value. Judge solely how reversible this decision is: "
                           "lock-in (vendor/format/API), exit costs, one-way vs. two-way door. How "
                           "expensive would backing out be in 12 months?",
            "effort": "medium",
        },
        {
            "role": "Team-Fit",
            "lens_prompt": "You judge the fit to team skills, existing conventions and learning curve. "
                           "Can the team sustainably operate/extend this, or does it create a "
                           "knowledge island?",
            "effort": "medium",
        },
        {
            "role": "Cost / TCO",
            "lens_prompt": "You judge total cost of ownership: build, operations, licensing, compute and "
                           "opportunity cost across the lifecycle — not just acquisition.",
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
