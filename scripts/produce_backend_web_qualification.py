#!/usr/bin/env python3
"""Executes installed backend/web qualification phases without granting release: one whole image-gate
suite group or the service, migration-recovery and real-authentication phase per protected job, or
every phase in one local pass. Each phase writes an honest receipt that the assemble phase joins into
the phase status and missing-check inventory; a complete evidence.json still requires the full trusted
collector.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]
BASELINE_PLANE = "4a07d59a448c1960ce2ae3f35e605f4d78c4a3f9"
BASELINE_IMAGE = "sha256:0061eecdbdc516f900a0d77221d806ac6f4d0c7e74cfa743eb5e0ce7b430c634"
FIXTURE = "109b0c2d812b18efa1481b7a2851e847711626173807048a33ebea4172a86e5a"
POSTGRES = "postgres@sha256:18cfe3ef5e6815560c98237d6216d1e5119702fb0f3894c8785dd58b8bbe5d73"
PHASES = ("all", "gate", "services", "assemble")
GATE_GROUPS = ("tests", "persistent_agents", "modules")
GATE_TIMEOUT_SECONDS = 25 * 60
PRODUCER_JOB = "qualify-backend-web"
SERVICE_PHASES = (
    ("fresh-synthetic-iam-lets", ()),
    ("live-baseline-upgrade-and-paired-recovery",
     ("baseline-migration", "retained-data", "backup-restore", "recovery", "audit-continuity")),
    ("real-application-authentication-and-owner-isolation",
     ("real-keycloak-auth", "session-renewal", "owner-authorization-denials")),
)
AUTHENTICATION = "authentication.json"
RECEIPT_KEYS = {
    "schema_version", "scope", "phase", "group", "candidate_sha", "candidate_image", "policy_commit",
    "protected_context", "run", "qualification_id", "qualification_namespace_retained",
    "phase_observations", "observed_checks", "release_authorized", "script_sha256",
}
PHASE_ERRORS = (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError)
HEX40 = re.compile(r"[0-9a-f]{40}")
IMAGE = re.compile(r"(?:sha256:[0-9a-f]{64}|[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64})")
QUALIFICATION_ID = re.compile(r"[0-9a-f]{32}")


def module(name):
    spec = importlib.util.spec_from_file_location("protected_bwq_" + name, ROOT / "scripts" / (name + ".py"))
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


def clean_environment():
    return {key: value for key, value in os.environ.items()
            if not key.startswith(("GITHUB_", "GH_", "ACTIONS_", "RUNNER_"))}


def execute(arguments, *, cwd, environment=None, timeout=300):
    result = subprocess.run(arguments, cwd=cwd, env=environment or clean_environment(),
                            capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError("fixed qualification command failed")
    return result


def observer_user():
    return f"{os.getuid()}:{os.getgid()}"


def script_digest():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def phase_job(phase):
    return {"gate": PRODUCER_JOB + "-gates", "services": PRODUCER_JOB + "-services"}.get(phase, PRODUCER_JOB)


def gate_phase_name(group):
    return "complete-backend-unit-gates-" + group


def receipt_name(phase, group=None):
    return f"phase-gate-{group}.json" if phase == "gate" else "phase-services.json"


def expected_receipts():
    return [*(("gate", group) for group in GATE_GROUPS), ("services", None)]


def run_identity(protected):
    if not protected:
        return None
    return {"run_id": os.environ.get("GITHUB_RUN_ID", ""), "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "")}


def fresh_directory(path):
    if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink() or path.exists():
        raise ValueError("fresh absolute private/output directories are required")
    if path.resolve() != path:
        raise ValueError("qualification roots must not traverse a symlink")


def verify_context(args):
    if (not HEX40.fullmatch(args.policy_commit or "")
            or not HEX40.fullmatch(args.candidate_sha or "")):
        raise ValueError("exact policy and candidate commits are required")
    if args.phase not in PHASES or (args.phase == "gate") != (args.group in GATE_GROUPS) or (
            args.phase != "gate" and args.group is not None):
        raise ValueError("a whole suite group is required exactly for the gate phase")
    services = args.phase in {"all", "services"}
    if (args.phase == "assemble") != (args.receipts is not None):
        raise ValueError("phase receipts are joined only by the assemble phase")
    if services and not (isinstance(args.port, int) and 1024 <= args.port <= 65535):
        raise ValueError("qualification port must be a bounded unprivileged port")
    if args.protected and (
        args.phase == "all"
        or os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or os.environ.get("GITHUB_JOB") != phase_job(args.phase)
        or os.environ.get("GITHUB_WORKFLOW_SHA") != args.policy_commit
        or os.environ.get("BACKEND_WEB_PRODUCER_SHA") != args.policy_commit
        or os.environ.get("RUNNER_NAME") != os.environ.get("BACKEND_WEB_PRODUCER_RUNNER")
        or not os.environ.get("BACKEND_WEB_PRODUCER_RUNNER")
        or not re.fullmatch(r"[1-9][0-9]*", os.environ.get("GITHUB_RUN_ID", ""))
        or not re.fullmatch(r"[1-9][0-9]*", os.environ.get("GITHUB_RUN_ATTEMPT", ""))
    ):
        raise ValueError("installed protected producer context is required")
    driver = module("run_backend_web_qualification")
    driver.verify_source(ROOT, args.policy_commit)
    if args.phase != "assemble":
        if args.source is None:
            raise ValueError("phase inputs are incomplete")
        driver.verify_source(args.source, args.candidate_sha)
        images = [args.candidate_image]
        if services:
            if args.baseline_plane_source is None:
                raise ValueError("phase inputs are incomplete")
            driver.verify_source(args.baseline_plane_source, BASELINE_PLANE)
            images += [args.lets_image, args.materializer_image]
        for image in images:
            if not isinstance(image, str) or not driver.IMAGE.fullmatch(image):
                raise ValueError("immutable images are required")
    fresh_directory(args.output)
    if services:
        fresh_directory(args.private_root)
        if (args.private_root == args.output or args.private_root in args.output.parents
                or args.output in args.private_root.parents):
            raise ValueError("private materials and public evidence must be separate trees")
    if args.phase == "assemble":
        receipts = args.receipts
        if (not receipts.is_absolute() or receipts.is_symlink() or not receipts.is_dir()
                or receipts.resolve() != receipts or receipts == args.output
                or receipts in args.output.parents or args.output in receipts.parents):
            raise ValueError("phase receipts must be one separate real directory")
    return driver


def attempt(observations, name, action):
    try:
        result = action()
    except PHASE_ERRORS:
        observations.append({"phase": name, "status": "failed", "exit_code": 1})
        return False, None
    observations.append({"phase": name, "status": "executed", "exit_code": 0})
    return True, result


def write_receipt(args, destination, phase, group, observations, completed, *,
                  qualification_id=None, retained=False):
    receipt = {
        "schema_version": 1, "scope": "backend-web", "phase": phase, "group": group,
        "candidate_sha": args.candidate_sha, "candidate_image": args.candidate_image,
        "policy_commit": args.policy_commit, "protected_context": args.protected,
        "run": run_identity(args.protected), "qualification_id": qualification_id,
        "qualification_namespace_retained": retained, "phase_observations": observations,
        "observed_checks": sorted(completed), "release_authorized": False,
        "script_sha256": script_digest(),
    }
    (destination / receipt_name(phase, group)).write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    return receipt


def gate_phase(args, group, destination):
    environment = {
        key: value for key, value in clean_environment().items() if not key.startswith("ASTRAL_GATE_")
    } | {
        "ASTRAL_GATE_GROUP": group,
        "ASTRAL_GATE_NAMESPACE": "ad-bwq-gates-" + uuid.uuid4().hex,
        "ASTRAL_GATE_POLICY_ROOT": str(ROOT),
    }
    observations = []
    attempt(observations, gate_phase_name(group), lambda: execute(
        ["bash", str(ROOT / "scripts/backend_web_image_gate.sh"), args.candidate_image, "tests"],
        cwd=args.source, environment=environment, timeout=GATE_TIMEOUT_SECONDS,
    ))
    return write_receipt(args, destination, "gate", group, observations, set())


def authenticate(args, identifier, initialized, material, destination):
    target = destination / AUTHENTICATION
    # Only the separate observer image may write this result
    execute([
        "docker", "run", "--rm", "--network", initialized["service_network"],
        "--user", observer_user(),
        "--label", "com.astraldeep.backend-web-observer=" + identifier,
        "--security-opt", "no-new-privileges:true", "--cap-drop", "ALL", "--read-only",
        "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=64m", "--entrypoint", "python3",
        "--mount", f"type=bind,src={ROOT / 'scripts/probe_backend_web_auth.py'},dst=/probe.py,readonly",
        "--mount", f"type=bind,src={material},dst=/materials,readonly",
        "--mount", f"type=bind,src={destination},dst=/evidence",
        args.materializer_image, "/probe.py", "--qualification-id", identifier,
        "--material-root", "/materials", "--output", "/evidence/" + AUTHENTICATION,
    ], cwd=ROOT)
    report = json.loads(target.read_text())
    if (report.get("qualification_id") != identifier or report.get("synthetic_only") is not True
            or any(report.get(key) != "passed" for key in (
                "real_application_keycloak_login_pkce", "login_state_denial", "application_session_renewal",
                "owner_read_denials", "logout_isolation", "chat_history_retention",
                "attachment_upload_metadata_retention", "attachment_owner_denials",
            ))):
        raise ValueError("independent authentication observation is incomplete")
    return report


def services_phase(args, driver, destination):
    services = module("initialize_backend_web_services")
    identifier = uuid.uuid4().hex
    observations = []
    completed = set()
    (initialize_name, _), (deploy_name, deploy_checks), (authenticate_name, authenticate_checks) = SERVICE_PHASES
    material = args.private_root / "materials"
    material.mkdir(mode=0o700)
    initialized_ok, initialized = attempt(observations, initialize_name, lambda: services.initialize(
        qualification_id=identifier, source=args.source, material_root=material,
        candidate_commit=args.candidate_sha, candidate_image=args.candidate_image,
        lets_image=args.lets_image, policy_root=ROOT, policy_commit=args.policy_commit,
        materializer_image=args.materializer_image,
        voice_image=args.voice_image, voice_closure_sha256=args.voice_closure_sha256,
        speech_runtime_env=args.speech_runtime_env,
    ))
    if initialized_ok:
        composition = json.loads((args.source / "config/astral-composition.json").read_text())
        deployment_args = argparse.Namespace(
            qualification_id=identifier, candidate_sha=args.candidate_sha, candidate_image=args.candidate_image,
            postgres_image=POSTGRES, plane_commit=composition["components"]["astral-plane"]["commit"],
            plane_source=args.source / "components/AstralPlane", baseline_plane_source=args.baseline_plane_source,
            fixture_sha256=FIXTURE, runtime_env=Path(initialized["runtime_fragment"]),
            material_root=Path(initialized["material_root"]), service_network=initialized["service_network"],
            port=args.port, output=args.private_root / "deployment.json", baseline_deep_image=BASELINE_IMAGE,
        )
        deployed_ok, _ = attempt(observations, deploy_name, lambda: driver.deploy(deployment_args))
        if deployed_ok:
            completed.update(deploy_checks)
            authenticated_ok, _ = attempt(observations, authenticate_name, lambda: authenticate(
                args, identifier, initialized, material, destination))
            if authenticated_ok:
                completed.update(authenticate_checks)
    return write_receipt(args, destination, "services", None, observations, completed,
                         qualification_id=identifier, retained=initialized_ok)


def validate_receipt(args, receipt, phase, group):
    if (type(receipt) is not dict or set(receipt) != RECEIPT_KEYS
            or receipt["schema_version"] != 1 or receipt["scope"] != "backend-web"
            or receipt["phase"] != phase or receipt["group"] != group
            or receipt["candidate_sha"] != args.candidate_sha
            or receipt["policy_commit"] != args.policy_commit
            or receipt["protected_context"] is not args.protected
            or receipt["run"] != run_identity(args.protected)
            or receipt["release_authorized"] is not False
            or receipt["script_sha256"] != script_digest()
            or not isinstance(receipt["candidate_image"], str)
            or not IMAGE.fullmatch(receipt["candidate_image"])):
        raise ValueError("qualification phase receipt identity differs")
    allowed = [(gate_phase_name(group), ())] if phase == "gate" else list(SERVICE_PHASES)
    observations = receipt["phase_observations"]
    if type(observations) is not list or not 0 < len(observations) <= len(allowed):
        raise ValueError("qualification phase receipt observations are malformed")
    checks = set()
    for index, observation in enumerate(observations):
        name, phase_checks = allowed[index]
        if type(observation) is not dict or type(observation.get("exit_code")) is not int:
            raise ValueError("qualification phase receipt observations are malformed")
        if observation == {"phase": name, "status": "executed", "exit_code": 0}:
            checks.update(phase_checks)
        elif observation != {"phase": name, "status": "failed", "exit_code": 1} or index != len(observations) - 1:
            raise ValueError("qualification phase receipt observations are malformed")
    if len(observations) < len(allowed) and observations[-1]["status"] != "failed":
        raise ValueError("an interrupted phase sequence must end in its failed phase")
    if receipt["observed_checks"] != sorted(checks):
        raise ValueError("qualification phase receipt claims unobserved checks")
    identifier, retained = receipt["qualification_id"], receipt["qualification_namespace_retained"]
    if phase == "gate":
        valid = identifier is None and retained is False
    else:
        valid = (isinstance(identifier, str) and QUALIFICATION_ID.fullmatch(identifier) is not None
                 and retained is (observations[0]["status"] == "executed"))
    if not valid:
        raise ValueError("qualification phase receipt namespace identity is malformed")
    return receipt


def load_receipts(args, receipts):
    names = {receipt_name(phase, group): (phase, group) for phase, group in expected_receipts()}
    found = {}
    for path in sorted(receipts.iterdir()):
        if path.name == AUTHENTICATION:
            continue
        if path.name not in names or path.is_symlink() or not path.is_file():
            raise ValueError("unexpected qualification phase receipt")
        phase, group = names[path.name]
        found[path.name] = validate_receipt(args, json.loads(path.read_text(encoding="utf-8")), phase, group)
    return found


def assemble_phase(args, receipts, destination):
    policy = module("validate_backend_web_evidence")
    found = load_receipts(args, receipts)
    services = found.get(receipt_name("services"))
    observations = []
    completed = set()
    images = set()
    for phase, group in expected_receipts():
        receipt = found.get(receipt_name(phase, group))
        if receipt is None:
            names = [gate_phase_name(group)] if phase == "gate" else [name for name, _ in SERVICE_PHASES]
            observations += [{"phase": name, "status": "missing", "exit_code": None} for name in names]
            continue
        observations += receipt["phase_observations"]
        completed.update(receipt["observed_checks"])
        images.add(receipt["candidate_image"])
    if len(images) > 1:
        raise ValueError("qualification phases tested different candidate images")
    authentication = receipts / AUTHENTICATION
    if authentication.exists() or authentication.is_symlink():
        if services is None or authentication.is_symlink() or not authentication.is_file():
            raise ValueError("authentication evidence lacks its service phase receipt")
        shutil.copyfile(authentication, destination / AUTHENTICATION)
    status = {
        "schema_version": 1, "scope": "backend-web",
        "qualification_id": services["qualification_id"] if services else None,
        "candidate_sha": args.candidate_sha, "candidate_image": next(iter(images), None),
        "policy_commit": args.policy_commit, "release_authorized": False,
        "protected_context": args.protected, "phase_observations": observations,
        "observed_checks": sorted(completed), "pending_checks": sorted(policy.CHECKS - completed),
        "status": "incomplete-not-qualified",
        "qualification_namespace_retained": bool(services and services["qualification_namespace_retained"]),
        "remaining_producer_work": [
            "Independently normalize all six complete test inventories, lint, history scan, integrity and changed coverage.",
            "Execute real provider/chat/tool/attachment/background and LETS success/denial/recovery collectors.",
            "Execute enabled browser voice/worker and responsive browser acceptance collectors.",
            "Bind all raw reports to the final image and generate the full consumer schema only after every required check passes.",
        ],
        "operator_inputs": ["Installed independent policy/runner/bootstrap image", "Approved in-product provider and voice worker service configuration"],
        "script_sha256": script_digest(),
    }
    (destination / "producer-status.json").write_text(json.dumps(status, sort_keys=True, indent=2) + "\n")
    return status


def produce(args):
    driver = verify_context(args)
    args.output.mkdir(mode=0o700, parents=True)
    if args.phase == "gate":
        return gate_phase(args, args.group, args.output)
    if args.phase == "assemble":
        return assemble_phase(args, args.receipts, args.output)
    args.private_root.mkdir(mode=0o700, parents=True)
    if args.phase == "services":
        return services_phase(args, driver, args.output)
    receipts = args.private_root / "receipts"
    receipts.mkdir(mode=0o700)
    for group in GATE_GROUPS:
        gate_phase(args, group, receipts)
    services_phase(args, driver, receipts)
    return assemble_phase(args, receipts, args.output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=PHASES, default="all")
    parser.add_argument("--group", choices=GATE_GROUPS)
    for name in ("policy-commit", "candidate-sha"):
        parser.add_argument("--" + name, required=True)
    for name in ("candidate-image", "lets-image", "materializer-image"):
        parser.add_argument("--" + name)
    parser.add_argument("--output", type=Path, required=True)
    for name in ("source", "baseline-plane-source", "private-root", "receipts"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument("--protected", action="store_true")
    parser.add_argument("--voice-image")
    parser.add_argument("--voice-closure-sha256")
    parser.add_argument("--speech-runtime-env", type=Path)
    args = parser.parse_args(argv)
    try:
        result = produce(args)
    except PHASE_ERRORS:
        print("protected backend/web producer refused its inputs or could not preserve isolated evidence", file=sys.stderr)
        return 1
    if args.phase in {"gate", "services"}:
        executed = (len(result["phase_observations"]) == (1 if args.phase == "gate" else len(SERVICE_PHASES))
                    and all(row["status"] == "executed" for row in result["phase_observations"]))
        print(json.dumps({"phase": args.phase, "group": args.group, "release_authorized": False,
                          "status": "executed" if executed else "failed"}))
        return 0 if executed else 2
    print(json.dumps({"status": result["status"], "release_authorized": False,
                      "pending_check_count": len(result["pending_checks"])}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
