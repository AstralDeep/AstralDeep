"""Verifies reusable Apple artifacts and App Store upload identities for the iPhone release workflow.
App Store queries are read-only; signing and upload remain in the protected macOS job."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import struct
import subprocess
import tarfile
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

BUNDLE = "com.personalailabs.astraldeep"
REPO = "AstralDeep/AstralDeep"
API = "https://api.appstoreconnect.apple.com/v1/"
PROJECTION_INFO = "apple-clients/AstralApp/Info.plist"
PROJECTION_CONSENT_TESTS = "apple-clients/AstralApp/AstralAppTests/MeshConsentTests.swift"
PROJECTION_LEDGER = "provenance/transformations.json"
PROJECTION_QUALIFICATION_PATHS = frozenset({
    "scripts/native_xccov_domain.py",
    "tests/test_native_xccov_domain.py",
    "apple-clients/AstralApp/AstralAppTests/AppModelServerCustodyTests.swift",
})
FACE_ID_TEST_ANCHOR = b"final class LocalAuthenticatorTests: XCTestCase {\n"
FACE_ID_HOST_BUNDLE_TEST = b'''    func testAppBundleDeclaresFaceIDPurposeForBiometricConsent() throws {
        let purpose = try XCTUnwrap(
            Bundle(for: AppModel.self).object(forInfoDictionaryKey: "NSFaceIDUsageDescription") as? String)
        XCTAssertFalse(purpose.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
    }

'''


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


class UniquePlistDictionary(dict):
    def __init__(self, pairs=()):
        super().__init__()
        for key, value in pairs:
            self[key] = value

    def __setitem__(self, key, value):
        require(key not in self, "Projection metadata contains a duplicate dictionary key")
        super().__setitem__(key, value)


def projection_git(*args):
    try:
        return subprocess.check_output(
            ["git", "--no-optional-locks", "--no-replace-objects", *args], stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        raise ValueError("Projection compatibility Git objects are unavailable") from None


def projection_blob(oid):
    require(projection_git("cat-file", "-t", oid).strip() == b"blob", "Projection path must refer to a blob")
    size = int(projection_git("cat-file", "-s", oid))
    require(0 < size <= 1024**2, "Projection compatibility blob exceeds bounds")
    return projection_git("cat-file", "blob", oid)


def reject_projection_json_constant(_value):
    raise ValueError("Projection provenance JSON contains a non-finite number")


def projection_ledger_compatibility(original, current, info_bytes):
    documents = [json.loads(payload, object_pairs_hook=UniquePlistDictionary,
                            parse_constant=reject_projection_json_constant) for payload in (original, current)]
    hashes = {}
    for label, document, info in zip(("original", "current"), documents, info_bytes, strict=True):
        require(isinstance(document, dict), "Projection provenance must be a dictionary")
        require(document.get("format") == "astral.extraction-transformations/v1"
                and isinstance(document.get("sourceManifestSha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", document["sourceManifestSha256"]),
                "Projection provenance format or source manifest binding is invalid")
        entries = document.get("entries")
        require(isinstance(entries, list) and all(isinstance(entry, dict) for entry in entries),
                "Projection provenance entries must be dictionaries")
        matches = [entry for entry in entries if entry.get("path") == PROJECTION_INFO]
        require(len(matches) == 1, "Projection provenance requires exactly one Info result binding")
        digest = hashlib.sha256(info).hexdigest()
        require(matches[0].get("resultSha256") == digest,
                "Projection provenance result hash does not bind the exact Info bytes")
        hashes[label] = digest
        del matches[0]["resultSha256"]
    canonical = [json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False) for document in documents]
    require(canonical[0] == canonical[1], "Projection provenance changed beyond the single Info result hash")
    return hashes


def projection_compatibility(original, current):
    require(all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) for value in (original, current)),
            "Projection compatibility requires immutable commit identities")
    proof = {"original_projection": original, "current_projection": current, "changed_paths": []}
    if original == current:
        return {**proof, "mode": "exact"}
    require(all(projection_git("cat-file", "-t", value).strip() == b"commit" for value in (original, current)),
            "Projection compatibility requires commit objects")
    ancestor = subprocess.run(
        ["git", "--no-optional-locks", "--no-replace-objects", "merge-base", "--is-ancestor", original, current],
        check=False, capture_output=True,
    )
    require(ancestor.returncode == 0, "producer Projection must be an ancestor of the current consumer")
    raw = projection_git("diff", "--raw", "--no-abbrev", "-z", "--no-renames", "--no-ext-diff",
                         "--no-textconv", "--ignore-submodules=none", original, current, "--")
    parts = raw.split(b"\0")
    require(parts[-1] == b"" and len(parts) % 2 == 1, "invalid Projection raw tree delta")
    changes = {}
    for header, path in zip(parts[:-1:2], parts[1:-1:2], strict=True):
        fields = header.split()
        require(len(fields) == 5 and fields[0] == b":100644" and fields[1] == b"100644" and fields[4] == b"M",
                "Projection compatibility permits only modified regular nonexecutable files")
        name = path.decode("utf-8")
        require(name in ({PROJECTION_INFO, PROJECTION_CONSENT_TESTS, PROJECTION_LEDGER}
                         | PROJECTION_QUALIFICATION_PATHS) and name not in changes,
                "Projection compatibility includes an unapproved path")
        changes[name] = (fields[2].decode("ascii"), fields[3].decode("ascii"))
    required_paths = {PROJECTION_INFO, PROJECTION_CONSENT_TESTS, PROJECTION_LEDGER}
    qualification_paths = set(changes) - required_paths
    require(required_paths <= set(changes) and qualification_paths <= PROJECTION_QUALIFICATION_PATHS,
            "Projection compatibility permits only the purpose binding and named qualification inputs")
    info_bytes = [projection_blob(oid) for oid in changes[PROJECTION_INFO]]
    old_info, new_info = [plistlib.loads(payload, dict_type=UniquePlistDictionary) for payload in info_bytes]
    require(isinstance(old_info, dict) and isinstance(new_info, dict), "Projection Info must be a dictionary")
    purpose = new_info.get("NSFaceIDUsageDescription")
    require("NSFaceIDUsageDescription" not in old_info and isinstance(purpose, str) and purpose.strip(),
            "Projection Info must only add a nonempty Face ID purpose")
    del new_info["NSFaceIDUsageDescription"]
    require(plistlib.dumps(old_info, fmt=plistlib.FMT_BINARY) == plistlib.dumps(new_info, fmt=plistlib.FMT_BINARY),
            "Projection Info changed beyond the Face ID purpose addition")
    old_tests, new_tests = [projection_blob(oid) for oid in changes[PROJECTION_CONSENT_TESTS]]
    require(old_tests.count(FACE_ID_TEST_ANCHOR) == 1
            and new_tests == old_tests.replace(FACE_ID_TEST_ANCHOR, FACE_ID_TEST_ANCHOR + FACE_ID_HOST_BUNDLE_TEST, 1),
            "Projection consent tests must preserve every byte except the exact host-bundle regression")
    ledger_bytes = [projection_blob(oid) for oid in changes[PROJECTION_LEDGER]]
    ledger_hashes = projection_ledger_compatibility(*ledger_bytes, info_bytes)
    return {**proof, "mode": "face-id-purpose-addition", "ancestor": True,
            "changed_paths": sorted(changes), "blob_changes": {
                name: {"original": pair[0], "current": pair[1]} for name, pair in sorted(changes.items())},
            "qualification_only_paths": sorted(qualification_paths),
            "info_only_adds_face_id_purpose": True, "tests_exact_host_bundle_regression": True,
            "provenance_only_updates_info_result": True, "info_result_sha256": ledger_hashes}


def framework_only_jobs(run):
    attempt = run.get("run_attempt")
    require(type(attempt) is int and 1 <= attempt <= 20, "invalid framework producer attempt")
    repository_id = run["repository"].get("id")
    require(type(repository_id) is int and repository_id > 0
            and run["head_repository"].get("id") == repository_id,
            "framework producer repository identity is invalid")
    expected = {
        "Decide the next Mac release": "success", "Require exact pinned Projection Apple CI": "success",
        "Build source-bound Apple framework": "success", "Build and qualify source-bound node pack": "skipped",
        "Build, notarize and publish the Mac DMG": "skipped",
    }
    response = github(f"actions/runs/{run['id']}/jobs?filter=all&per_page=100")
    require(isinstance(response, dict) and isinstance(response.get("jobs"), list)
            and type(response.get("total_count")) is int
            and 0 < response["total_count"] == len(response["jobs"]) <= 100,
            "framework producer job graph is malformed or exceeds bounds")
    latest, identities, occurrences = {}, set(), set()
    for job in response["jobs"]:
        require(isinstance(job, dict) and type(job.get("id")) is int and job["id"] > 0
                and job["id"] not in identities and isinstance(job.get("name"), str) and job["name"] in expected
                and job.get("run_id") == run["id"] and job.get("head_sha") == run["head_sha"]
                and type(job.get("run_attempt")) is int and 1 <= job["run_attempt"] <= attempt
                and job.get("status") == "completed"
                and isinstance(job.get("conclusion"), str)
                and job["conclusion"] in {"success", "failure", "skipped", "cancelled", "timed_out",
                                               "action_required", "neutral", "stale", "startup_failure"},
                "framework producer job identity or completion is invalid")
        require(expected[job["name"]] != "skipped" or job["conclusion"] == "skipped",
                "framework producer must skip Mac packaging and publication in every attempt")
        occurrence = (job["name"], job["run_attempt"])
        require(occurrence not in occurrences, "duplicate framework producer job attempt")
        occurrences.add(occurrence)
        identities.add(job["id"])
        if job["name"] not in latest or latest[job["name"]]["run_attempt"] < job["run_attempt"]:
            latest[job["name"]] = job
    require(set(latest) == set(expected)
            and any(job["run_attempt"] == attempt for job in latest.values())
            and all(job["conclusion"] == expected[name] for name, job in latest.items()),
            "manual Apple producer is not a successful framework-only job graph")
    steps = latest["Decide the next Mac release"].get("steps")
    require(isinstance(steps, list) and all(isinstance(item, dict) for item in steps),
            "framework decision steps are malformed")
    markers = [item for item in steps if item.get("name") == "Confirm framework-only source decision"]
    require(len(markers) == 1 and markers[0].get("status") == "completed"
            and markers[0].get("conclusion") == "success", "missing successful framework-only source decision")
    return {"run_attempt": attempt, "jobs": {
        name: {key: job[key] for key in ("id", "run_attempt", "conclusion")} for name, job in latest.items()}}


def kernel_qualification(kernel):
    repo = "AstralDeep/AstralKernel"
    response = json.loads(command(
        "gh", "api", f"repos/{repo}/actions/workflows/ci.yml/runs?head_sha={kernel}&event=push&branch=k%2Fintegration&per_page=100",
    ))
    require(isinstance(response, dict) and isinstance(response.get("workflow_runs"), list)
            and len(response["workflow_runs"]) <= 100
            and all(isinstance(item, dict) for item in response["workflow_runs"]),
            "malformed Kernel qualification response")
    for item in response["workflow_runs"]:
        if (type(item.get("id")) is int and item["id"] > 0
                and type(item.get("workflow_id")) is int and item["workflow_id"] > 0
                and type(item.get("run_attempt")) is int and item["run_attempt"] > 0
                and item.get("head_sha") == kernel and item.get("head_branch") == "k/integration"
                and item.get("event") == "push" and item.get("path") == ".github/workflows/ci.yml"
                and isinstance(item.get("repository"), dict) and item["repository"].get("full_name") == repo
                and isinstance(item.get("head_repository"), dict) and item["head_repository"].get("full_name") == repo
                and item.get("status") == "completed" and item.get("conclusion") == "success"):
            return {key: item[key] for key in ("id", "workflow_id", "run_attempt", "head_sha", "head_branch",
                                              "event", "path", "repository", "head_repository", "status", "conclusion")}
    raise ValueError("no successful exact-source Kernel qualification on k/integration")


def producer(run_id, artifact_id):
    run = github(f"actions/runs/{run_id}")
    require(isinstance(run, dict) and isinstance(run.get("repository"), dict)
            and isinstance(run.get("head_repository"), dict), "malformed Apple producer identity")
    require(
        run.get("id") == run_id and run.get("workflow_id") == 397620410
        and run.get("path") == ".github/workflows/release-macos.yml"
        and run.get("event") in ("workflow_run", "workflow_dispatch")
        and run.get("head_branch") == "main" and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and run.get("repository", {}).get("full_name") == REPO
        and run.get("head_repository", {}).get("full_name") == REPO,
        "Apple producer must be a successful default-branch run of the approved workflow",
    )
    sha = run.get("head_sha", "")
    require(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha), "invalid producer source")
    manual = {"provider_event": "workflow_dispatch", "framework_only": framework_only_jobs(run)} if run["event"] == "workflow_dispatch" else {}
    subprocess.run(["git", "fetch", "--no-tags", "origin", sha], check=True)
    require(gitlink("HEAD", "AstralKernel") == gitlink(sha, "AstralKernel"), "producer component mismatch")
    projection = gitlink("HEAD", "AstralProjection")
    original_projection = gitlink(sha, "AstralProjection")
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
    if original_projection != projection:
        subprocess.run([
            "git", "fetch", "--no-tags", "--no-write-fetch-head",
            "https://github.com/AstralDeep/AstralProjection.git", original_projection, projection,
        ], check=True)
    compatibility = projection_compatibility(original_projection, projection)
    if manual:
        manual["kernel_qualification"] = kernel_qualification(gitlink("HEAD", "AstralKernel"))
        require(github(f"actions/runs/{run_id}") == run, "framework producer metadata changed during verification")
    return {"producer_run": run_id, "producer_sha": sha, "artifact_id": artifact_id,
            "artifact_digest": artifact["digest"], "projection": projection,
            "original_projection": original_projection, "current_projection": projection,
            "projection_compatibility": compatibility,
            "kernel": gitlink("HEAD", "AstralKernel"), **manual}


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
    platforms = profile.get("Platform", [])
    platform_tags = set(platforms)
    ios_family = "iOS" in platform_tags and platform_tags <= {"iOS", "xrOS", "visionOS"}
    checks = {"name_matches": profile.get("Name") == name,
              "team_matches": profile.get("TeamIdentifier") == [team],
              "app_id_matches": app_id == f"{team}.{bundle}",
              "platform_tags_allowed": platform_tags <= {"iOS", "watchOS"} or ios_family,
              "platform_nonempty": bool(profile.get("Platform"))}
    public_tags = ("iOS", "watchOS", "macOS", "tvOS", "xrOS", "visionOS")
    print(json.dumps({"profile_identity": checks,
                      "known_platform_tags": [tag for tag in public_tags if tag in platforms],
                      "unknown_platform_tags_present": any(tag not in public_tags for tag in platforms)}),
          flush=True)
    require(all(checks.values()), "provisioning profile identity mismatch: "
            + ", ".join(key for key, matches in checks.items() if not matches))
    require(profile["ExpirationDate"].replace(tzinfo=UTC) > datetime.now(UTC)
            and entitlements.get("get-task-allow") is False
            and not profile.get("ProvisionedDevices") and not profile.get("ProvisionsAllDevices"),
            "provisioning profile must be unexpired App Store distribution")


def apple_double_metadata(name, payload):
    path = Path(name)
    if (path.is_absolute() or ".." in path.parts or "\\" in name
            or not path.name.startswith("._") or len(path.name) <= 2):
        return False
    require(len(payload) >= 26, "truncated AppleDouble profile metadata")
    magic, version, _filler, count = struct.unpack_from(">II16sH", payload)
    require(magic == 0x00051607 and version == 0x00020000 and 0 < count <= 50,
            "invalid AppleDouble profile metadata header")
    header_end = 26 + 12 * count
    require(header_end <= len(payload), "truncated AppleDouble profile metadata entries")
    identifiers = set()
    ranges = []
    for index in range(count):
        identifier, offset, length = struct.unpack_from(">III", payload, 26 + 12 * index)
        require(identifier in {2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 13, 14, 15}
                and identifier not in identifiers and offset >= header_end
                and offset + length <= len(payload), "invalid AppleDouble profile metadata entry")
        identifiers.add(identifier)
        if length:
            ranges.append((offset, offset + length))
    ranges.sort()
    require(all(left[1] <= right[0] for left, right in pairwise(ranges)),
            "overlapping AppleDouble profile metadata entries")
    return True


def install_profiles(blob, destination):
    import zipfile

    decoded = blob.read_bytes()
    payloads = []
    summary = {"container": "raw", "entries": 1, "profile_payloads": 0,
               "appledouble_skipped": 0, "other_entries": 0, "directories": 0}
    if zipfile.is_zipfile(blob):
        with zipfile.ZipFile(blob) as archive:
            require(len(archive.infolist()) <= 50, "too many profile archive members")
            summary.update(container="zip", entries=len(archive.infolist()))
            for item in archive.infolist():
                require(item.file_size <= 1024**2, "profile exceeds size limit")
                if item.is_dir():
                    summary["directories"] += 1
                elif item.filename.endswith((".mobileprovision", ".provisionprofile")):
                    payloads.append((item.filename, archive.read(item)))
                else:
                    summary["other_entries"] += 1
    elif tarfile.is_tarfile(blob):
        with tarfile.open(blob) as archive:
            members = archive.getmembers()
            require(len(members) <= 50, "too many profile archive members")
            summary.update(container="tar", entries=len(members))
            for item in members:
                require(item.size <= 1024**2, "profile exceeds size limit")
                if item.isfile() and item.name.endswith((".mobileprovision", ".provisionprofile")):
                    payloads.append((item.name, archive.extractfile(item).read()))
                elif item.isdir():
                    summary["directories"] += 1
                else:
                    summary["other_entries"] += 1
    else:
        require(len(decoded) <= 1024**2, "profile exceeds size limit")
        payloads.append(("profile.mobileprovision", decoded))
    print(json.dumps({"container": summary["container"], "entries": summary["entries"],
                      "profile_candidates": len(payloads)}), flush=True)
    profiles = []
    for index, (name, payload) in enumerate(payloads):
        try:
            metadata = apple_double_metadata(name, payload)
        except ValueError:
            print(json.dumps({"entry": index, "classification": "invalid_appledouble",
                              "bytes": len(payload)}), flush=True)
            raise
        classification = "appledouble_metadata" if metadata else "profile_payload"
        print(json.dumps({"entry": index, "classification": classification,
                          "bytes": len(payload)}), flush=True)
        if metadata:
            summary["appledouble_skipped"] += 1
        else:
            summary["profile_payloads"] += 1
            profiles.append(payload)
    print(json.dumps(summary), flush=True)
    matches = {}
    for index, payload in enumerate(profiles):
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
        if target == app:
            purpose = info.get("NSFaceIDUsageDescription")
            require(isinstance(purpose, str) and purpose.strip(), "exported iPhone app must explain Face ID approval")
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
