"""Runs every selected acceptance mutation against the real isolated PostgreSQL proposal lifecycle.
The backend-web modules group owns this service-dependent effectiveness gate.
"""

from scripts.check_acceptance_mutations import MUTATIONS, qualify


def test_all_acceptance_mutations_are_detected_with_real_postgres(tmp_path):
    receipt = qualify(tmp_path / "acceptance-mutations.json")
    assert receipt["baseline"]["status"] == "passed", "focused baseline did not qualify"
    assert len(receipt["mutations"]) == len(MUTATIONS)
    assert receipt["status"] == "passed", (
        "an acceptance mutation survived or failed to execute"
    )
    assert all(record["status"] == "killed" for record in receipt["mutations"])
