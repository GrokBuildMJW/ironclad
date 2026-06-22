"""Unit test for the example `reverse` skill — pure, no engine.

Doubles as the gate's required sibling: `ack.sdk.gate` looks for `<package>/tests/test_<stem>.py`,
so shipping this file is what makes the example pass its own documented gate (see the README)."""

from ironclad_example_plugin.skills.reverse import CASE, run


def test_case_is_well_formed():
    assert CASE["name"] == "reverse" and CASE["capability"] == "reverse"
    assert isinstance(CASE["description"], str) and CASE["description"]


def test_reverse():
    assert run("hello") == "olleh"
    assert run("") == ""
    assert run("a") == "a"
    assert run("level") == "level"
