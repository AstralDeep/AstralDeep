"""Creates an isolated backend/browser qualification Docker deployment — real Keycloak,
LETS-enforce, and voice workers via an operator-owned env file — without ever
authorizing release; failed deployments stay up for diagnosis.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

IMAGE = re.compile(r"^(?:sha256:[0-9a-f]{64}|[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64})$")
SHA = re.compile(r"^[0-9a-f]{40}$")
ID = re.compile(r"^[0-9a-f]{32}$")
BASELINE_PLANE = "4a07d59a448c1960ce2ae3f35e605f4d78c4a3f9"
BASELINE_DIGEST = "a3d3ac43bee48b0ca6832cca1e4a347db0a3f838af908b8545edb11e7e94272a"
BASELINE_DEEP = "013a06922759ae737b24b20ab02705e35614789d"
BASELINE_IMAGE = "sha256:0061eecdbdc516f900a0d77221d806ac6f4d0c7e74cfa743eb5e0ce7b430c634"
LABEL = "com.astraldeep.backend-web-qualification"
SERVICES_LABEL = "com.astraldeep.backend-web-services"
APP_LIMITS = ["--cpus", "2", "--memory", "4g", "--pids-limit", "512"]
POSTGRES_LIMITS = ["--cpus", "2", "--memory", "2g", "--pids-limit", "256"]


class QualificationError(ValueError):
    pass


def run(arguments: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=300)
    if check and result.returncode:
        raise QualificationError(f"{arguments[0]} operation failed (exit {result.returncode})")
    return result


def wait_application(port: int, *, attempts: int = 120) -> dict[str, Any]:
    for _ in range(attempts):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request("GET", "/readyz")
            response = connection.getresponse()
            body = response.read(65537)
            if response.status == 200 and len(body) <= 65536:
                report = json.loads(body)
                if (isinstance(report, dict) and report.get("status") == "ok"
                        and report.get("db") == "ok" and isinstance(report.get("lets"), dict)
                        and report["lets"].get("mode") == "enforce"
                        and report["lets"].get("status") == "healthy"
                        and report["lets"].get("governed_dispatch_ready") is True):
                    return {"status": "ready", "lets_mode": "enforce", "acceptance_qualified": False}
        except (OSError, http.client.HTTPException, ValueError):
            pass
        finally:
            connection.close()
        time.sleep(1)
    raise QualificationError("isolated application did not reach LETS-enforce readiness")


def read_runtime(path: Path) -> dict[str, str]:
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise QualificationError("runtime environment must be an absolute regular protected file")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise QualificationError("runtime environment must be accessible only by its owner")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        key, separator, value = raw.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
            raise QualificationError("runtime environment contains malformed or duplicate entries")
        if "\x00" in value or value[:1] in {"'", '"'}:
            raise QualificationError("runtime environment must use literal Docker env-file values")
        values[key] = value
    if values.get("ASTRAL_ENV") != "production":
        raise QualificationError("qualification requires explicit production posture")
    if values.get("USE_MOCK_AUTH", "false").lower() != "false":
        raise QualificationError("qualification forbids mock authentication")
    if values.get("LETS_MODE") != "enforce":
        raise QualificationError("qualification requires LETS enforce")
    if values.get("FF_LETS_EXTERNAL_WARDEN", "").lower() != "true":
        raise QualificationError("qualification requires the configured external LETS warden")
    return values


def verify_source(path: Path, expected: str) -> Path:
    if not SHA.fullmatch(expected):
        raise QualificationError("component commit must be one exact Git SHA")
    root = path.resolve(strict=True)
    observed = run(["git", "-C", str(root), "rev-parse", "HEAD"]).stdout.strip()
    dirty = run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"]).stdout
    if observed != expected or dirty:
        raise QualificationError("component source must be the clean exact pinned commit")
    return root


def write_env(path: Path, values: dict[str, str]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        for key, value in values.items():
            if any(character in value for character in ("\n", "\r", "\x00")):
                raise QualificationError("environment contains multiline/control values")
            stream.write(f"{key}={value}\n")


def validate_material(root: Path, qualification_id: str) -> Path:
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise QualificationError("qualification material must be an absolute real directory")
    for path in (root, *root.rglob("*")):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise QualificationError("qualification material contains a link or unsupported object")
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise QualificationError("qualification material must be accessible only by its owner")
    marker = json.loads((root / "qualification.json").read_text(encoding="utf-8"))
    if marker != {
        "schema_version": 1, "qualification_id": qualification_id, "classification": "synthetic",
    }:
        raise QualificationError("qualification material is not bound to this synthetic namespace")
    return root.resolve()


# DB recovery never rewinds LETS replay/authority volumes
def _state_rehearsal(
    args: argparse.Namespace, *, prefix: str, network: str, app_env: Path, pg_env: Path,
    material: Path, evidence: Path, database: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    label = f"{LABEL}={args.qualification_id}"
    roots = {name: f"{prefix}-{name}" for name in ("data", "tmp", "knowledge", "agents")}
    helper = Path(__file__).with_name("rehearse_backend_web_state.py").resolve(strict=True)
    evidence.mkdir(mode=0o700)

    def mounts(selected: dict[str, str], *, readonly: bool = False) -> list[str]:
        result = []
        for name, volume in selected.items():
            result += ["--mount", f"type=volume,src={volume},dst=/app/backend/{name}" + (",readonly" if readonly else "")]
        return result

    def state(image: str, phase: str, selected: dict[str, str], checkpoint: str | None = None) -> dict[str, Any]:
        name = f"{prefix}-{phase}"
        command = [
            "docker", "create", *APP_LIMITS, "--name", name, "--label", label, "--network", network,
            "--env-file", str(app_env), "--entrypoint", "python3",
            "--mount", f"type=bind,src={helper},dst=/qualification-state.py,readonly",
            "--mount", f"type=bind,src={evidence},dst=/qualification-evidence",
            "--mount", f"type=bind,src={material},dst=/run/astral-bwq,readonly",
            "--mount", f"type=volume,src={prefix}-lets-replay,dst=/var/lib/astral-lets-replay",
            "--mount", f"type=volume,src={prefix}-lets-authority,dst=/var/lib/astral-lets-authority",
            *mounts(selected), image, "/qualification-state.py",
            "--qualification-id", args.qualification_id,
            "--output", f"/qualification-evidence/{phase}.json",
        ]
        if checkpoint:
            command += ["--checkpoint", f"/qualification-evidence/{checkpoint}", "--append"]
        run(command)
        run(["docker", "network", "connect", args.service_network, name])
        run(["docker", "start", "--attach", name])
        if run(["docker", "inspect", "--format", "{{.State.ExitCode}}", name]).stdout.strip() != "0":
            raise QualificationError("synthetic state rehearsal process failed")
        observed = json.loads((evidence / f"{phase}.json").read_text(encoding="utf-8"))
        if (observed.get("synthetic_only") is not True or observed.get("release_authorized") is not False
                or any(observed.get(key) != "pass" for key in (
                    "credential_decryption", "conversation_retention", "owner_denials", "authenticated_audit_continuity",
                ))):
            raise QualificationError("synthetic state rehearsal did not verify retained state")
        return observed

    seeded = state(args.baseline_deep_image, "baseline-state", {key: value for key, value in roots.items() if key != "agents"})
    run([
        "docker", "run", *APP_LIMITS, "--rm", "--label", label, "--network", "none", "--entrypoint", "true",
        "--mount", f"type=volume,src={roots['agents']},dst=/app/backend/agents", args.candidate_image,
    ])
    dump = evidence / "baseline.dump"
    archive = evidence / "baseline-blobs.tar"
    run(["docker", "exec", f"{prefix}-pg", "pg_dump", "-U", "qualification", "-d", database,
         "--format=custom", "--file=/tmp/backend-web-baseline.dump"])
    run(["docker", "cp", f"{prefix}-pg:/tmp/backend-web-baseline.dump", str(dump)])
    run([
        "docker", "run", *APP_LIMITS, "--rm", "--label", label, "--network", "none", "--entrypoint", "tar",
        "--mount", f"type=bind,src={evidence},dst=/qualification-evidence", *mounts(roots, readonly=True),
        args.candidate_image, "--directory=/", "--create", "--file=/qualification-evidence/baseline-blobs.tar",
        *(f"app/backend/{name}" for name in roots),
    ])
    for path in (dump, archive):
        if path.is_symlink() or not path.is_file() or not path.stat().st_size:
            raise QualificationError("paired backup is incomplete")
        path.chmod(0o600)
    backups = {}
    for path in (dump, archive):
        with path.open("rb") as stream:
            backups[path.name] = hashlib.file_digest(stream, "sha256").hexdigest()
    migrated = state(args.candidate_image, "candidate-state", roots, "baseline-state.json")
    for path in (dump, archive):
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != backups[path.name]:
                raise QualificationError("paired backup bytes changed before recovery")
    # Fresh cluster required; --clean can't add missing tables
    network = f"{prefix}-recovery-network"
    recovered_pg = f"{prefix}-recovered-pg"
    run(["docker", "network", "create", "--label", label, network])
    run(["docker", "volume", "create", "--label", label, recovered_pg])
    run([
        "docker", "run", *POSTGRES_LIMITS, "-d", "--name", recovered_pg, "--label", label,
        "--network", network, "--network-alias", f"{prefix}-pg", "--env-file", str(pg_env),
        "--mount", f"type=volume,src={recovered_pg},dst=/var/lib/postgresql/data", args.postgres_image,
    ])
    for attempt in range(60):
        if run(["docker", "exec", recovered_pg, "pg_isready", "-U", "qualification", "-d", database], check=False).returncode == 0:
            break
        time.sleep(1)
    else:
        raise QualificationError("fresh recovery PostgreSQL did not become ready")
    owner = json.loads(run(["docker", "container", "inspect", recovered_pg]).stdout)[0]
    if (owner.get("Config", {}).get("Labels") or {}).get(LABEL) != args.qualification_id:
        raise QualificationError("qualification PostgreSQL ownership changed before recovery")
    run(["docker", "cp", str(dump), f"{recovered_pg}:/tmp/backend-web-recovery.dump"])
    run(["docker", "exec", recovered_pg, "pg_restore", "-U", "qualification", "-d", database,
         "--exit-on-error", "--single-transaction", "/tmp/backend-web-recovery.dump"])
    recovered_roots = {name: f"{prefix}-recovered-{name}" for name in roots}
    for volume in recovered_roots.values():
        run(["docker", "volume", "create", "--label", label, volume])
    run([
        "docker", "run", *APP_LIMITS, "--rm", "--label", label, "--network", "none", "--entrypoint", "tar",
        "--mount", f"type=bind,src={evidence},dst=/qualification-evidence,readonly", *mounts(recovered_roots),
        args.candidate_image, "--directory=/", "--extract", "--file=/qualification-evidence/baseline-blobs.tar",
    ])
    recovered = state(args.candidate_image, "recovered-state", recovered_roots, "baseline-state.json")
    if (migrated["checkpoint"] != seeded["checkpoint"] or recovered["checkpoint"] != seeded["checkpoint"]):
        raise QualificationError("state checkpoint changed across upgrade or recovery")
    return {
        "status": "synthetic-state-and-paired-recovery-passed", "baseline_deep_commit": BASELINE_DEEP,
        "baseline_image": BASELINE_IMAGE, "candidate_image": args.candidate_image,
        "evidence_directory": str(evidence), "backup_sha256": backups,
        "reports": ["baseline-state.json", "candidate-state.json", "recovered-state.json"],
        "helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
        "application_network": network, "recovered_postgres_container": recovered_pg,
        "preserved_upgraded_postgres_container": f"{prefix}-pg", "restore_target": "new-empty-postgres-cluster",
        "bundled_agents_source": "exact-candidate-image-initialized-volume",
        "legacy_mutable_generated_agent_directories_rehearsed": False,
        "lets_replay_authority_rewound": False, "release_authorized": False,
        "post_backup_candidate_append_discarded_by_rehearsed_restore": migrated["appended_event_id"],
        "real_provider_called": False,
    }, recovered_roots


def deploy(args: argparse.Namespace) -> dict[str, Any]:
    if not ID.fullmatch(args.qualification_id) or not SHA.fullmatch(args.candidate_sha):
        raise QualificationError("qualification ID and candidate SHA must be exact")
    if not IMAGE.fullmatch(args.candidate_image) or not IMAGE.fullmatch(args.postgres_image):
        raise QualificationError("candidate and PostgreSQL images must be immutable digest identities")
    if not 1024 <= args.port <= 65535:
        raise QualificationError("host port must be in 1024..65535")
    runtime = read_runtime(args.runtime_env)
    material = validate_material(args.material_root, args.qualification_id)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", args.service_network):
        raise QualificationError("service network must have one bounded explicit Docker identity")
    service_network = json.loads(run(["docker", "network", "inspect", args.service_network]).stdout)[0]
    if (service_network.get("Labels") or {}).get(SERVICES_LABEL) != args.qualification_id:
        raise QualificationError("service network is not owned by this qualification namespace")
    plane = verify_source(args.plane_source, args.plane_commit)
    baseline = verify_source(args.baseline_plane_source, BASELINE_PLANE)
    if not re.fullmatch(r"[0-9a-f]{64}", args.fixture_sha256):
        raise QualificationError("fixture identity must be one exact SHA-256")
    candidate = json.loads(run(["docker", "image", "inspect", args.candidate_image]).stdout)[0]
    labels = candidate.get("Config", {}).get("Labels") or {}
    if labels.get("org.opencontainers.image.revision") != args.candidate_sha:
        raise QualificationError("candidate image revision label differs from the exact candidate")
    image_plane = run([
        "docker", "run", *APP_LIMITS, "--rm", "--network", "none", "--entrypoint", "python3",
        args.candidate_image, "-c",
        "import json; print(json.load(open('/app/config/astral-composition.json'))"
        "['components']['astral-plane']['commit'])",
    ]).stdout.strip()
    if image_plane != args.plane_commit:
        raise QualificationError("candidate image Plane pin differs from the qualified import source")
    run([
        "docker", "run", *APP_LIMITS, "--rm", "--network", "none", "--entrypoint", "python3",
        args.candidate_image, "/app/scripts/install_local_components.py", "verify",
        "--root", "/app", "--lock", "/opt/astral-component-wheels/astral-component-wheels.lock.json",
    ])
    baseline_image = getattr(args, "baseline_deep_image", None)
    if baseline_image:
        if not IMAGE.fullmatch(baseline_image):
            raise QualificationError("baseline Deep image must be immutable")
        observed_baseline = json.loads(run(["docker", "image", "inspect", baseline_image]).stdout)[0]
        if observed_baseline.get("Id") != BASELINE_IMAGE:
            raise QualificationError("baseline Deep image differs from the inventoried live image")
        observed_pin = run([
            "docker", "run", *APP_LIMITS, "--rm", "--network", "none", "--entrypoint", "python3", baseline_image, "-c",
            "import json; print(json.load(open('/app/config/astral-composition.json'))['components']['astral-plane']['commit'])",
        ]).stdout.strip()
        if observed_pin != BASELINE_PLANE:
            raise QualificationError("baseline Deep image Plane pin differs")
    prefix = f"ad-bwq-{args.qualification_id}"
    for kind in ("container", "network", "volume"):
        existing = run(
            ["docker", kind, "ls", "-q", "--filter", f"label={LABEL}={args.qualification_id}"]
        ).stdout.strip()
        if existing:
            raise QualificationError("qualification identity already owns Docker resources")
    for kind, suffixes in (
        ("container", ("pg", "app", "baseline-state", "candidate-state", "recovered-state", "recovered-pg")),
        ("network", ("network", "recovery-network")),
        ("volume", ("pg", "data", "tmp", "knowledge", "agents", "lets-replay", "lets-authority",
                    "recovered-data", "recovered-tmp", "recovered-knowledge", "recovered-agents", "recovered-pg")),
    ):
        for suffix in suffixes:
            if run(["docker", kind, "inspect", f"{prefix}-{suffix}"], check=False).returncode == 0:
                raise QualificationError("qualification Docker resource name already exists")
    output = args.output.resolve()
    if output.exists():
        raise QualificationError("diagnostic evidence output must be a new file")
    state_evidence = output.parent / f"{prefix}-state"
    if state_evidence.exists() or state_evidence.is_symlink():
        raise QualificationError("state rehearsal evidence directory must be new")
    output.parent.mkdir(parents=True, exist_ok=True)
    label = f"{LABEL}={args.qualification_id}"
    network = f"{prefix}-network"
    database = f"astralplane_qualification_{args.qualification_id}"
    password = secrets.token_hex(32)
    schema = f"astralplane_fixture_{args.qualification_id}"
    dsn = f"postgresql://qualification:{password}@{prefix}-pg:5432/{database}"
    configured_dsn = dsn + "?options=" + quote(f"-csearch_path={schema},pg_catalog", safe="")
    attachments = "/app/backend/tmp/attachments"
    run(["docker", "network", "create", "--label", label, network])
    for suffix in ("pg", "data", "tmp", "knowledge", "agents", "lets-replay", "lets-authority"):
        run(["docker", "volume", "create", "--label", label, f"{prefix}-{suffix}"])
    with tempfile.TemporaryDirectory(prefix="astral-bwq-") as temporary:
        private = Path(temporary)
        pg_env = private / "postgres.env"
        import_env = private / "import.env"
        app_env = private / "runtime.env"
        write_env(pg_env, {
            "POSTGRES_USER": "qualification", "POSTGRES_PASSWORD": password, "POSTGRES_DB": database,
        })
        write_env(import_env, {"ASTRALPLANE_QUALIFICATION_DATABASE_URL": dsn})
        write_env(app_env, {
            **runtime, "DATABASE_URL": configured_dsn, "DB_HOST": f"{prefix}-pg",
            "DB_PORT": "5432", "DB_NAME": database, "DB_USER": "qualification",
            "DB_PASSWORD": password, "ATTACHMENT_UPLOAD_ROOT": attachments,
            "PERSONAL_AGENT_ARTIFACT_ROOT": "/app/backend/data/personal-agent-artifacts",
        })
        run([
            "docker", "run", *POSTGRES_LIMITS, "-d", "--name", f"{prefix}-pg", "--label", label,
            "--network", network, "--env-file", str(pg_env),
            "--mount", f"type=volume,src={prefix}-pg,dst=/var/lib/postgresql/data",
            args.postgres_image,
        ])
        for attempt in range(60):
            ready = run([
                "docker", "exec", f"{prefix}-pg", "pg_isready", "-U", "qualification", "-d", database,
            ], check=False)
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise QualificationError("isolated PostgreSQL did not become ready")
        importer = [
            "docker", "run", *APP_LIMITS, "--rm", "--label", label, "--network", network,
            "--env-file", str(import_env), "--entrypoint", "python3",
            "--mount", f"type=bind,src={plane},dst=/qualification-plane,readonly",
            "--mount", f"type=bind,src={baseline},dst=/baseline-plane,readonly",
            "--mount", f"type=volume,src={prefix}-tmp,dst=/app/backend/tmp",
        ]
        fixture = json.loads(run(importer + [
            args.candidate_image, "/qualification-plane/scripts/import_staging_fixture.py",
            "--qualification-id", args.qualification_id,
            "--expected-fixture-sha256", args.fixture_sha256, "--blob-root", attachments,
        ]).stdout)
        baseline_report = json.loads(run(importer + [
            "-e", "PYTHONPATH=/baseline-plane/src", args.candidate_image,
            "/qualification-plane/scripts/migrate_qualification_database.py",
            "--qualification-id", args.qualification_id, "--expected-revision", "088.003",
            "--expected-migration-digest", BASELINE_DIGEST,
        ]).stdout)
        state_report = None
        app_roots = {name: f"{prefix}-{name}" for name in ("data", "tmp", "knowledge", "agents")}
        if baseline_image:
            state_report, app_roots = _state_rehearsal(
                args, prefix=prefix, network=network, app_env=app_env, pg_env=pg_env,
                material=material, evidence=state_evidence, database=database,
            )
            network = state_report["application_network"]
        application = [
            "docker", "create", *APP_LIMITS, "--name", f"{prefix}-app", "--label", label,
            "--network", network, "--env-file", str(app_env),
            "-p", f"127.0.0.1:{args.port}:8001",
            "--mount", f"type=bind,src={material},dst=/run/astral-bwq,readonly",
            "--mount", f"type=volume,src={prefix}-lets-replay,dst=/var/lib/astral-lets-replay",
            "--mount", f"type=volume,src={prefix}-lets-authority,dst=/var/lib/astral-lets-authority",
        ]
        for suffix, volume in app_roots.items():
            application += ["--mount", f"type=volume,src={volume},dst=/app/backend/{suffix}"]
        run(application + [args.candidate_image])
        run(["docker", "network", "connect", args.service_network, f"{prefix}-app"])
        run(["docker", "start", f"{prefix}-app"])
        readiness = wait_application(args.port)
    report = {
        "schema_version": 1, "scope": "backend-web", "qualification_id": args.qualification_id,
        "candidate_sha": args.candidate_sha, "candidate_image": args.candidate_image,
        "candidate_image_id": candidate["Id"], "plane_commit": args.plane_commit,
        "baseline_plane_commit": BASELINE_PLANE, "baseline": baseline_report,
        "fixture": fixture, "runtime_env_sha256": hashlib.sha256(args.runtime_env.read_bytes()).hexdigest(),
        "postgres_image": args.postgres_image, "docker_namespace": prefix,
        "service_network": args.service_network,
        "infrastructure_readiness": readiness,
        "state_rehearsal": state_report, "application_blob_volumes": app_roots,
        "loopback_endpoint": f"http://127.0.0.1:{args.port}",
        "status": "started-acceptance-required", "release_authorized": False,
        "required_acceptance": [
            "production-readiness-and-workers", "real-authentication-and-session-renewal",
            "backend-web-owner-security-and-functional-flows", "lets-enforce-success-denial-recovery",
            "enabled-browser-voice", "paired-backup-restore", "protected-independent-decision",
        ],
    }
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("qualification-id", "candidate-sha", "candidate-image", "postgres-image", "plane-commit", "fixture-sha256", "service-network"):
        parser.add_argument(f"--{name}", required=True)
    for name in ("plane-source", "baseline-plane-source", "runtime-env", "output", "material-root"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--baseline-deep-image", help="Exact live baseline image enables synthetic key/audit and paired backup recovery rehearsal")
    try:
        report = deploy(parser.parse_args())
    except QualificationError as exc:
        print(f"qualification deployment refused: {exc}")
        return 2
    except (OSError, ValueError, subprocess.SubprocessError):
        print("qualification deployment failed; existing/live resources were not selected; inspect the labelled isolated namespace")
        return 2
    print(json.dumps({key: report[key] for key in ("qualification_id", "status", "release_authorized")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
