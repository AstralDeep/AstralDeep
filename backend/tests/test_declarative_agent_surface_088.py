"""Tests for the declarative-agent chrome dispatch surface
(orchestrator/projection_surfaces/authoring.py, guidance.py): the view/command
handlers over real IAM, owner scoping, and exact-receipt replay.
"""

from __future__ import annotations


from orchestrator.projection_surfaces import authoring, guidance


def test_handlers_are_registered_under_their_wire_names():
    # Registered in guidance.HANDLERS, not authoring.HANDLERS
    assert "chrome_declarative_view" not in authoring.HANDLERS
    assert "chrome_declarative_command" not in authoring.HANDLERS
    assert guidance.HANDLERS["chrome_declarative_view"] is authoring._h_declarative_view
    assert guidance.HANDLERS["chrome_declarative_command"] is authoring._h_declarative_command
    from orchestrator.projection_surfaces import collect_handlers
    aggregated = collect_handlers()
    assert aggregated["chrome_declarative_view"] == ("guidance", authoring._h_declarative_view)
    assert aggregated["chrome_declarative_command"] == ("guidance", authoring._h_declarative_command)


