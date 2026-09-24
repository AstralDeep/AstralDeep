#!/usr/bin/env python3
"""Executes installed backend/web qualification phases without granting release,
producing an honest phase receipt and missing-check inventory; a complete
evidence.json still requires the full trusted collector.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]
BASELINE_PLANE = "4a07d59a448c1960ce2ae3f35e605f4d78c4a3f9"
BASELINE_IMAGE = "sha256:0061eecdbdc516f900a0d77221d806ac6f4d0c7e74cfa743eb5e0ce7b430c634"
FIXTURE = "109b0c2d812b18efa1481b7a2851e847711626173807048a33ebea4172a86e5a"
POSTGRES = "postgres@sha256:18cfe3ef5e6815560c98237d6216d1e5119702fb0f3894c8785dd58b8bbe5d73"


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


def verify_context(args):
    if (not re.fullmatch(r"[0-9a-f]{40}", args.policy_commit)
            or not re.fullmatch(r"[0-9a-f]{40}", args.candidate_sha)):
        raise ValueError("exact policy and candidate commits are required")
    if not 1024 <= args.port <= 65535:
        raise ValueError("qualification port must be a bounded unprivileged port")
    if args.protected and (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or os.environ.get("GITHUB_JOB") != "qualify-backend-web"
        or os.environ.get("GITHUB_WORKFLOW_SHA") != args.policy_commit
        or os.environ.get("BACKEND_WEB_PRODUCER_SHA") != args.policy_commit
        or os.environ.get("RUNNER_NAME") != os.environ.get("BACKEND_WEB_PRODUCER_RUNNER")
        or not os.environ.get("BACKEND_WEB_PRODUCER_RUNNER")
    ):
        raise ValueError("installed protected producer context is required")
    driver = module("run_backend_web_qualification")
    driver.verify_source(ROOT, args.policy_commit)
    driver.verify_source(args.source, args.candidate_sha)
    driver.verify_source(args.baseline_plane_source, BASELINE_PLANE)
    for image in (args.candidate_image, args.lets_image, args.materializer_image):
        if not driver.IMAGE.fullmatch(image):
            raise ValueError("immutable images are required")
    for path in (args.private_root, args.output):
        if not path.is_absolute() or path.is_symlink() or path.exists():
            raise ValueError("fresh absolute private/output directories are required")
        if path.resolve() != path:
            raise ValueError("qualification roots must not traverse a symlink")
    if args.private_root == args.output or args.private_root in args.output.parents or args.output in args.private_root.parents:
        raise ValueError("private materials and public evidence must be separate trees")
    return driver


def produce(args):
    driver = verify_context(args)
    policy = module("validate_backend_web_evidence")
    services = module("initialize_backend_web_services")
    identifier = uuid.uuid4().hex
    args.private_root.mkdir(mode=0o700, parents=True)
    args.output.mkdir(mode=0o700, parents=True)
    observations = []
    completed = set()

    def phase(name, action, checks=()):
        try:
            result = action()
            observations.append({"phase": name, "status": "executed", "exit_code": 0})
            completed.update(checks)
            return result
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError):
            observations.append({"phase": name, "status": "failed", "exit_code": 1})
            return None

    gate_environment = clean_environment() | {
        "ASTRAL_GATE_NAMESPACE": "ad-bwq-gates-" + identifier,
        "ASTRAL_GATE_POLICY_ROOT": str(ROOT),
    }
    phase("complete-backend-unit-gates", lambda: execute(
        ["bash", str(ROOT / "scripts/backend_web_image_gate.sh"), args.candidate_image, "tests"],
        cwd=args.source, environment=gate_environment, timeout=8 * 3600,
    ))
    material = args.private_root / "materials"
    material.mkdir(mode=0o700)
    initialized = phase("fresh-synthetic-iam-lets", lambda: services.initialize(
        qualification_id=identifier, source=args.source, material_root=material,
        candidate_commit=args.candidate_sha, candidate_image=args.candidate_image,
        lets_image=args.lets_image, policy_root=ROOT, policy_commit=args.policy_commit,
        materializer_image=args.materializer_image,
        voice_image=args.voice_image, voice_closure_sha256=args.voice_closure_sha256,
        speech_runtime_env=args.speech_runtime_env,
    ))
    deployed = None
    if initialized is not None:
        composition = json.loads((args.source / "config/astral-composition.json").read_text())
        deployment_args = argparse.Namespace(
            qualification_id=identifier, candidate_sha=args.candidate_sha, candidate_image=args.candidate_image,
            postgres_image=POSTGRES, plane_commit=composition["components"]["astral-plane"]["commit"],
            plane_source=args.source / "components/AstralPlane", baseline_plane_source=args.baseline_plane_source,
            fixture_sha256=FIXTURE, runtime_env=Path(initialized["runtime_fragment"]),
            material_root=Path(initialized["material_root"]), service_network=initialized["service_network"],
            port=args.port, output=args.private_root / "deployment.json", baseline_deep_image=BASELINE_IMAGE,
        )
        deployed = phase("live-baseline-upgrade-and-paired-recovery", lambda: driver.deploy(deployment_args),
                         ("baseline-migration", "retained-data", "backup-restore", "recovery", "audit-continuity"))
        if deployed is not None:
            # Only the separate observer image may write this result
            def authentication():
                target = args.output / "authentication.json"
                execute([
                    "docker", "run", "--rm", "--network", initialized["service_network"],
                    "--user", observer_user(),
                    "--label", "com.astraldeep.backend-web-observer=" + identifier,
                    "--security-opt", "no-new-privileges:true", "--cap-drop", "ALL", "--read-only",
                    "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=64m", "--entrypoint", "python3",
                    "--mount", f"type=bind,src={ROOT / 'scripts/probe_backend_web_auth.py'},dst=/probe.py,readonly",
                    "--mount", f"type=bind,src={material},dst=/materials,readonly",
                    "--mount", f"type=bind,src={args.output},dst=/evidence",
                    args.materializer_image, "/probe.py", "--qualification-id", identifier,
                    "--material-root", "/materials", "--output", "/evidence/authentication.json",
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
            phase("real-application-authentication-and-owner-isolation", authentication,
                  ("real-keycloak-auth", "session-renewal", "owner-authorization-denials"))
    status = {
        "schema_version": 1, "scope": "backend-web", "qualification_id": identifier,
        "candidate_sha": args.candidate_sha, "candidate_image": args.candidate_image,
        "policy_commit": args.policy_commit, "release_authorized": False,
        "protected_context": args.protected, "phase_observations": observations,
        "observed_checks": sorted(completed), "pending_checks": sorted(policy.CHECKS - completed),
        "status": "incomplete-not-qualified", "qualification_namespace_retained": initialized is not None,
        "remaining_producer_work": [
            "Independently normalize all six complete test inventories, lint, history scan, integrity and changed coverage.",
            "Execute real provider/chat/tool/attachment/background and LETS success/denial/recovery collectors.",
            "Execute enabled browser voice/worker and responsive browser acceptance collectors.",
            "Bind all raw reports to the final image and generate the full consumer schema only after every required check passes.",
        ],
        "operator_inputs": ["Installed independent policy/runner/bootstrap image", "Approved in-product provider and voice worker service configuration"],
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    (args.output / "producer-status.json").write_text(json.dumps(status, sort_keys=True, indent=2) + "\n")
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("policy-commit", "candidate-sha", "candidate-image", "lets-image", "materializer-image"):
        parser.add_argument("--" + name, required=True)
    for name in ("source", "baseline-plane-source", "private-root", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--protected", action="store_true")
    parser.add_argument("--voice-image")
    parser.add_argument("--voice-closure-sha256")
    parser.add_argument("--speech-runtime-env", type=Path)
    try:
        status = produce(parser.parse_args(argv))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError):
        print("protected backend/web producer refused its inputs or could not preserve isolated evidence", file=sys.stderr)
        return 1
    print(json.dumps({"status": status["status"], "release_authorized": False,
                      "pending_check_count": len(status["pending_checks"])}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
