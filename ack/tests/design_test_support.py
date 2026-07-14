"""Shared design-lifecycle setup for tests whose subject requires implementation staging."""


def approve_active_design(gx10):
    """Establish the approved-design precondition for the active test project."""
    slug = gx10.active_slug()
    if gx10._design_gate("implementation", slug) is None:
        return
    gx10.record_design("Approved test design", "Implement the staged test unit as described.")
    approval = gx10._approve_design()
    assert approval.startswith("OK"), approval
    assert gx10._design_gate("implementation", slug) is None
