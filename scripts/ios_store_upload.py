"""Verifies reusable Apple artifacts and App Store upload identities for the iPhone release workflow.
App Store queries are read-only; signing and upload remain in the protected macOS job."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import subprocess
import tarfile
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

BUNDLE = "com.personalailabs.astraldeep"
REPO = "AstralDeep/AstralDeep"
API = "https://api.appstoreconnect.apple.com/v1/"


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def github(path):
    return json.loads(command("gh", "api", f"repos/{REPO}/{path}"))


def gitlink(ref, component):
    row = command("git", "ls-tree", ref, "--", f"components/{component}").split()
    require(len(row) == 4 and row[:2] == ["160000", "commit"], "missing component gitlink")
    return row[2]


def producer(run_id, artifact_id):
    run = github(f"actions/runs/{run_id}")
    require(
        run.get("id") == run_id and run.get("workflow_id") == 397620410
        and run.get("path") == ".github/workflows/release-macos.yml"
        and run.get("event") == "workflow_run"
        and run.get("head_branch") == "main" and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and run.get("repository", {}).get("full_name") == REPO
        and run.get("head_repository", {}).get("full_name") == REPO,
        "Apple producer must be a successful default-branch run of the approved workflow",
    )
    sha = run.get("head_sha", "")
    require(re.fullmatch(r"[0-9a-f]{40}", sha), "invalid producer source")
    subprocess.run(["git", "fetch", "--no-tags", "origin", sha], check=True)
    for component in ("AstralProjection", "AstralKernel"):
        require(gitlink("HEAD", component) == gitlink(sha, component), "producer component mismatch")
    projection = gitlink("HEAD", "AstralProjection")
    response = json.loads(command(
        "gh", "api", f"repos/AstralDeep/AstralProjection/actions/workflows/apple-ci.yml/runs?head_sha={projection}&event=push&branch=main&per_page=100",
    ))
    require(any(
        item.get("head_sha") == projection and item.get("head_branch") == "main"
        and item.get("event") == "push" and item.get("path") == ".github/workflows/apple-ci.yml"
        and item.get("repository", {}).get("full_name") == "AstralDeep/AstralProjection"
        and item.get("head_repository", {}).get("full_name") == "AstralDeep/AstralProjection"
        and item.get("status") == "completed" and item.get("conclusion") == "success"
        for item in response["workflow_runs"]
    ), "no successful exact-source Projection Apple CI")
    artifact = github(f"actions/artifacts/{artifact_id}")
    require(
        artifact.get("id") == artifact_id and artifact.get("expired") is False
        and artifact.get("name") == f"macos-apple-artifacts-{sha}"
        and artifact.get("workflow_run", {}).get("id") == run_id
        and artifact.get("workflow_run", {}).get("head_sha") == sha
        and artifact.get("workflow_run", {}).get("repository_id") == run["repository"]["id"]
        and artifact.get("workflow_run", {}).get("head_repository_id") == run["repository"]["id"]
        and re.fullmatch(r"sha256:[0-9a-f]{64}", artifact.get("digest", "")),
        "Apple artifact identity or provider digest mismatch",
    )
    return {"producer_run": run_id, "producer_sha": sha, "artifact_id": artifact_id,
            "artifact_digest": artifact["digest"], "projection": projection,
            "kernel": gitlink("HEAD", "AstralKernel")}


def verify_download(path, receipt):
    actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    require(actual == receipt["artifact_digest"], "downloaded artifact provider digest mismatch")


def restore_tar(path, destination):
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        require(len(members) <= 4096 and sum(item.size for item in members) <= 2 * 1024**3,
                "Apple framework archive exceeds bounds")
        names = set()
        for item in members:
            parts = Path(item.name).parts
            require(parts and parts[0] == "Artifacts" and not Path(item.name).is_absolute()
                    and ".." not in parts and (item.isfile() or item.isdir())
                    and item.name not in names, "unsafe Apple framework archive member")
            names.add(item.name)
            require(not (destination / item.name).exists(), "framework restore would overwrite files")
        archive.extractall(destination, members=members)


def asc_pages(url, token):
    result = []
    seen = set()
    while url:
        require(url.startswith(API) and url not in seen and len(seen) < 1000,
                "invalid App Store pagination")
        seen.add(url)
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
        require(isinstance(payload.get("data"), list), "invalid App Store response")
        result.extend(payload["data"])
        url = payload.get("links", {}).get("next")
    return result


def release_identity(version, build, builds):
    require(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version), "invalid marketing version")
    require(re.fullmatch(r"[1-9][0-9]{0,3}", build), "build must be an integer from 1 to 9999")
    prior = []
    for item in builds:
        value = item["attributes"]["version"]
        require(isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,3}", value),
                "prior iOS build uses an unsupported numbering format")
        prior.append(int(value))
    require(int(build) > max(prior, default=0), "build must exceed every uploaded iOS build")
    return {"version": version, "build": build, "highest_uploaded_ios_build": max(prior, default=0)}


def preflight(version, build, key_path):
    from jose import jwt

    now = int(time.time())
    token = jwt.encode(
        {"iss": os.environ["ASC_ISSUER_ID"], "iat": now, "exp": now + 600,
         "aud": "appstoreconnect-v1"},
        key_path.read_text(), algorithm="ES256", headers={"kid": os.environ["ASC_KEY_ID"]},
    )
    apps = asc_pages(API + "apps?" + urllib.parse.urlencode({"filter[bundleId]": BUNDLE}), token)
    require(len(apps) == 1 and apps[0]["attributes"]["bundleId"] == BUNDLE,
            "App Store bundle must resolve to exactly one application")
    app = apps[0]["id"]
    builds = asc_pages(API + "builds?" + urllib.parse.urlencode(
        {"filter[app]": app, "filter[preReleaseVersion.platform]": "IOS", "limit": 200}), token)
    return {"app_id": app, **release_identity(version, build, builds)}


def profile_identity(profile, bundle, name, team):
    entitlements = profile.get("Entitlements", {})
    app_id = entitlements.get("application-identifier")
    require(profile.get("Name") == name and profile.get("TeamIdentifier") == [team]
            and app_id == f"{team}.{bundle}"
            and set(profile.get("Platform", [])) <= {"iOS", "watchOS"}
            and bool(profile.get("Platform")), "provisioning profile identity mismatch")
    require(profile["ExpirationDate"].replace(tzinfo=UTC) > datetime.now(UTC)
            and entitlements.get("get-task-allow") is False
            and not profile.get("ProvisionedDevices") and not profile.get("ProvisionsAllDevices"),
            "provisioning profile must be unexpired App Store distribution")


def install_profiles(blob, destination):
    import zipfile

    decoded = blob.read_bytes()
    payloads = []
    if zipfile.is_zipfile(blob):
        with zipfile.ZipFile(blob) as archive:
            require(len(archive.infolist()) <= 50, "too many profile archive members")
            for item in archive.infolist():
                require(item.file_size <= 1024**2, "profile exceeds size limit")
                if item.filename.endswith((".mobileprovision", ".provisionprofile")):
                    payloads.append(archive.read(item))
    elif tarfile.is_tarfile(blob):
        with tarfile.open(blob) as archive:
            members = archive.getmembers()
            require(len(members) <= 50, "too many profile archive members")
            for item in members:
                require(item.size <= 1024**2, "profile exceeds size limit")
                if item.isfile() and item.name.endswith((".mobileprovision", ".provisionprofile")):
                    payloads.append(archive.extractfile(item).read())
    else:
        payloads.append(decoded)
    matches = {}
    for index, payload in enumerate(payloads):
        temporary = blob.parent / f"decoded-profile-{index}.mobileprovision"
        temporary.write_bytes(payload)
        profile = plistlib.loads(subprocess.check_output(["security", "cms", "-D", "-i", str(temporary)]))
        for bundle, variable in ((BUNDLE, "APPLE_PROFILE_IOS"), (BUNDLE + ".watch", "APPLE_PROFILE_WATCH")):
            if profile.get("Name") == os.environ[variable]:
                profile_identity(profile, bundle, os.environ[variable], os.environ["APPLE_TEAM_ID"])
                require(bundle not in matches, "ambiguous provisioning profiles")
                matches[bundle] = payload
        temporary.unlink()
    require(set(matches) == {BUNDLE, BUNDLE + ".watch"}, "iPhone and Watch App Store profiles required")
    destination.mkdir(parents=True, exist_ok=True)
    for index, payload in enumerate(matches.values()):
        (destination / f"astral-ios-{index}.mobileprovision").write_bytes(payload)


def verify_app(app, version, build):
    watch = app / "Watch" / "AstralWatch.app"
    require(watch.is_dir(), "embedded Watch app is required")
    for target, bundle in ((app, BUNDLE), (watch, BUNDLE + ".watch")):
        info = plistlib.loads((target / "Info.plist").read_bytes())
        require(info.get("CFBundleIdentifier") == bundle
                and info.get("CFBundleShortVersionString") == version
                and info.get("CFBundleVersion") == build, "iPhone/Watch release versions disagree")
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(target)], check=True)
        entitlements = plistlib.loads(subprocess.check_output(
            ["codesign", "-d", "--entitlements", ":-", str(target)], stderr=subprocess.DEVNULL))
        require(entitlements.get("application-identifier") == f"{os.environ['APPLE_TEAM_ID']}.{bundle}"
                and entitlements.get("get-task-allow", False) is False,
                "exported app signing entitlements mismatch")
        profile = plistlib.loads(subprocess.check_output(
            ["security", "cms", "-D", "-i", str(target / "embedded.mobileprovision")]))
        variable = "APPLE_PROFILE_WATCH" if target == watch else "APPLE_PROFILE_IOS"
        profile_identity(profile, bundle, os.environ[variable], os.environ["APPLE_TEAM_ID"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("producer", "restore", "preflight", "profiles", "verify-app"))
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--artifact-id", type=int)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--version")
    parser.add_argument("--build")
    args = parser.parse_args()
    if args.command == "producer":
        args.receipt.write_text(json.dumps(producer(args.run_id, args.artifact_id), indent=2) + "\n")
    elif args.command == "restore":
        receipt = json.loads(args.receipt.read_text())
        verify_download(args.input, receipt)
        from extract_release_artifact import extract_artifact

        temporary = args.input.parent / "unpacked"
        extract_artifact(args.input, temporary, expected_members=["apple-artifacts.tar.gz"])
        restore_tar(temporary / "apple-artifacts.tar.gz", args.destination)
    elif args.command == "preflight":
        args.receipt.write_text(json.dumps(preflight(args.version, args.build, args.input), indent=2) + "\n")
    elif args.command == "profiles":
        install_profiles(args.input, args.destination)
    else:
        verify_app(args.input, args.version, args.build)


if __name__ == "__main__":
    main()
