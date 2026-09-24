"""CLI entry point for `python -m verification`; an operator or CI job runs the harness
(backend/verification/runner.py, drivers/in_process.py, report.py) and exits with a
code distinguishing pass, fail, and near-exposure.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any, Dict, List, Optional

from verification.config import RunConfig
from verification.verdict import Outcome, Verdict, reconcile


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="verification", description="AstralDeep SDUI & "
                                "delegated-authority verification harness")
    p.add_argument("--mode", default="in-process",
                   help="in-process (scripted LLM, CI gate) or external (live + Keycloak)")
    p.add_argument("--persona", action="append", default=[], dest="personas",
                   help="restrict to a persona key (repeatable)")
    p.add_argument("--base-url", default=None, help="external-mode target base URL")
    p.add_argument("--out", default=None, help="gitignored artifacts root")
    p.add_argument("--run-id", default=None, help="namespace for principals + artifacts")
    p.add_argument("--llm-judge", action="store_true", help="enable optional LLM-as-judge")
    p.add_argument("--strict", action="store_true", help="any uncertain -> non-zero exit")
    p.add_argument("--quiet", action="store_true", help="suppress progress narration")
    p.add_argument("--stamp", default="local", help="timestamp/nonce for the run id")
    return p.parse_args(argv)


def config_from_args(args: argparse.Namespace) -> RunConfig:
    run_id = args.run_id or f"__verif__{args.stamp}"
    kwargs: Dict[str, Any] = {
        "mode": args.mode,
        "run_id": run_id,
        "personas": list(args.personas or []),
        "base_url": args.base_url,
        "strict": bool(args.strict),
        "llm_judge": bool(args.llm_judge),
    }
    if args.out:
        kwargs["out_dir"] = args.out
    return RunConfig(**kwargs)


def _verdict_for(check, ev, inputs, scenario) -> Verdict:
    res = check.run(ev, inputs)
    refuted = check.counter_refutes(ev, inputs) if res.outcome == Outcome.PASS else False
    outcome, confidence, adversarial = reconcile(res.outcome, refuted, None)
    refs = {"check": check.check_id, "property": check.property,
            "counter_check": f"{check.check_id}.counter"}
    if scenario is not None:
        refs.update({"persona": scenario.persona.key, "scenario": scenario.scenario_id})
    else:
        refs.update({"persona": ev.scenario_id.split(":")[0], "scenario": ev.scenario_id})
    return Verdict(
        verdict_id=f"{refs['scenario']}:{check.check_id}", scope="check",
        outcome=outcome, run_mode=ev.run_mode, confidence=confidence,
        evidence_ref=ev.evidence_id, refs=refs, adversarial=adversarial, reason=res.reason,
    )


async def run_in_process(config: RunConfig) -> Dict[str, Any]:
    from verification.checks import authority as A
    from verification.checks.tangible_ui import build_us1_checks
    from verification.checks.thin_client import build_us3_checks
    from verification.drivers.in_process import InProcessDriver
    from verification.report import build_record
    from verification.scenarios import build_scenarios

    driver = InProcessDriver(config)
    await driver.setup()
    verdicts: List[Verdict] = []
    evidence: Dict[str, Any] = {}
    persona_keys: List[str] = []
    try:
        scenarios = build_scenarios(config.run_id, "mock_inprocess",
                                    config.personas or None)
        persona_keys = sorted({s.persona.key for s in scenarios})
        us1, us3 = build_us1_checks(), build_us3_checks()
        for scenario in scenarios:
            ev = await driver.run_scenario(scenario)
            ev = driver.enrich_thin_client(ev)
            ev = ev.redacted(config.secret_values())
            evidence[scenario.scenario_id] = ev
            inputs = {"warrants_ui": scenario.warrants_ui,
                      "known_markers": list(scenario.persona.fixture.known_markers),
                      "query": scenario.query}
            for check in us1 + us3:
                verdicts.append(_verdict_for(check, ev, inputs, scenario))
        probes = {
            "xuser": await driver.probe_cross_user(config.run_id),
            "scope": await driver.probe_scope_withheld(config.run_id),
            "deleg": driver.probe_delegation(config.run_id),
            "appr": await driver.probe_admin_approval(config.run_id),
        }
        for check, key in [
            (A.CROSS_USER, "xuser"), (A.DENIALS, "xuser"), (A.CHAIN, "xuser"),
            (A.SCOPE, "scope"), (A.DELEGATION, "deleg"), (A.APPROVAL, "appr"),
        ]:
            ev = probes[key].redacted(config.secret_values())
            evidence[ev.scenario_id] = ev
            verdicts.append(_verdict_for(check, ev, {}, None))
    finally:
        await driver.teardown()
    return build_record(config, verdicts, evidence, auth_mode="mock_inprocess",
                        personas=persona_keys)


def exit_code_for(record: Dict[str, Any], strict: bool) -> int:
    flags = record.get("flags", [])
    if "credential_near_exposure" in flags:
        return 2
    outcomes = [v.get("outcome") for v in record.get("verdicts", [])]
    if any(o == "fail" for o in outcomes):
        return 1
    if strict and any(o == "uncertain" for o in outcomes):
        return 2
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    config = config_from_args(args)
    from verification.report import write_report

    if config.mode == "external":
        from verification.drivers.external import ExternalDriver  # noqa: F401
        raise SystemExit("external mode requires a live deployment; run with a base URL "
                         "and network access (not a CI gate)")

    record = asyncio.run(run_in_process(config))
    paths = write_report(record, config.run_dir)
    if not args.quiet:
        n = len(record["verdicts"])
        npass = sum(1 for v in record["verdicts"] if v["outcome"] == "pass")
        print(f"[verification] {npass}/{n} checks passed · "
              f"uncertain={record['uncertain_ratio']} · report={paths['markdown']}")
    return exit_code_for(record, config.strict)


if __name__ == "__main__":
    sys.exit(main())
