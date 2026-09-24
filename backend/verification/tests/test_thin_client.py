"""Tests for backend-only-UI checks (backend/verification/checks/thin_client.py,
drivers/in_process.py, AstralProjection webrender): the real client.js has no
construction logic, and delivered components stay in the published vocabulary.
"""

from __future__ import annotations

from verification.checks.thin_client import build_us3_checks, inspect_client_surface
from verification.drivers.in_process import InProcessDriver
from verification.scenarios import BACKEND_ONLY_UI, build_scenarios
from verification.verdict import Outcome, reconcile
from verification.tests.conftest import INTEGRATION, run_async

pytestmark = INTEGRATION


def _run_everyday(run_config):
    async def _go():
        driver = InProcessDriver(run_config)
        await driver.setup()
        try:
            scenario = build_scenarios(
                run_config.run_id, "mock_inprocess", ["everyday"], properties=[BACKEND_ONLY_UI]
            )[0]
            ev = await driver.run_scenario(scenario)
            return driver.enrich_thin_client(ev)
        finally:
            await driver.teardown()

    return run_async(_go())


def _verdict(check, ev):
    res = check.run(ev, {})
    refuted = check.counter_refutes(ev, {}) if res.outcome == Outcome.PASS else False
    outcome, _c, _a = reconcile(res.outcome, refuted, None)
    return outcome, res.reason


def test_client_surface_has_no_construction_logic():
    insp = inspect_client_surface()
    assert insp.get("readable"), f"client.js unreadable: {insp}"
    assert not insp["framework_import"], "client must not import a rendering framework"
    assert not insp["type_switch"], "client must not construct components by type"
    assert insp["injects_server_html"], "client must inject server HTML generically"
    assert insp["forwards_actions"], "client must forward ui_event actions"


def test_us3_backend_only_ui(run_config):
    ev = _run_everyday(run_config)
    checks = build_us3_checks()
    results = {c.check_id: _verdict(c, ev) for c in checks}
    fails = {k: v[1] for k, v in results.items() if v[0] == Outcome.FAIL}
    assert not fails, f"US3 FAIL verdicts: {fails}"
    for required in ("us3.no_client_construction", "us3.server_markup_present", "vocabulary_ok"):
        assert results[required][0] == Outcome.PASS, (
            f"{required}: {results[required][1]}"
        )

    from webrender import allowed_primitive_types

    allowed = set(allowed_primitive_types())
    types = {c.get("type") for c in ev.components if isinstance(c, dict)}
    assert types <= allowed, f"out-of-vocabulary components: {types - allowed}"
