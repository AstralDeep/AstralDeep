"""Exercises iPhone upload provenance, App Store identity, profile and payload denials.
Workflow contracts are verified separately in the release workflow suite."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import plistlib
import tarfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("ios_store_upload", ROOT / "scripts/ios_store_upload.py")
upload = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upload)


@pytest.fixture
def provider(monkeypatch):
    repo = {"full_name": upload.REPO, "id": 10}
    run = {"id": 11, "workflow_id": 397620410, "path": ".github/workflows/release-macos.yml",
           "event": "workflow_run", "head_branch": "main", "status": "completed", "conclusion": "success",
           "repository": repo, "head_repository": repo, "head_sha": "a" * 40}
    artifact = {"id": 22, "name": "macos-apple-artifacts-" + "a" * 40, "expired": False,
                "digest": "sha256:" + "b" * 64,
                "workflow_run": {"id": 11, "head_sha": "a" * 40, "repository_id": 10, "head_repository_id": 10}}
    ci = {"workflow_runs": [{"head_sha": "c" * 40, "head_branch": "main", "event": "push",
                            "path": ".github/workflows/apple-ci.yml", "status": "completed",
                            "conclusion": "success", "repository": {"full_name": "AstralDeep/AstralProjection"},
                            "head_repository": {"full_name": "AstralDeep/AstralProjection"}}]}
    monkeypatch.setattr(upload, "github", lambda path: run if path.startswith("actions/runs") else artifact)
    monkeypatch.setattr(upload, "gitlink", lambda ref, component: "c" * 40)
    monkeypatch.setattr(upload, "command", lambda *args: json.dumps(ci))
    monkeypatch.setattr(upload.subprocess, "run", lambda *args, **kwargs: None)
    return run, artifact, ci


def test_producer(provider):
    result = upload.producer(11, 22)
    assert result["producer_sha"] == "a" * 40
    assert result["artifact_digest"] == "sha256:" + "b" * 64


@pytest.mark.parametrize("field,value", [
    ("workflow_id", 8), ("path", "evil.yml"), ("event", "pull_request"),
    ("head_branch", "candidate"), ("status", "in_progress"), ("conclusion", "failure"),
    ("repository", {"full_name": "attacker/fork"}), ("head_repository", {"full_name": "attacker/fork"}),
    ("head_sha", "main"), ("id", 12),
])
def test_producer_refuses_wrong_run(provider, field, value):
    provider[0][field] = value
    with pytest.raises(ValueError):
        upload.producer(11, 22)


@pytest.mark.parametrize("field,value", [
    ("id", 23), ("expired", True), ("name", "other"), ("digest", None),
    ("workflow_run", {"id": 12}),
])
def test_producer_refuses_wrong_artifact(provider, field, value):
    provider[1][field] = value
    with pytest.raises((ValueError, TypeError)):
        upload.producer(11, 22)


def test_producer_refuses_unqualified_source(provider, monkeypatch):
    provider[2]["workflow_runs"][0]["conclusion"] = "failure"
    with pytest.raises(ValueError, match="Projection"):
        upload.producer(11, 22)
    monkeypatch.setattr(upload, "gitlink", lambda ref, component: ref)
    with pytest.raises(ValueError, match="component mismatch"):
        upload.producer(11, 22)


def test_gitlink_command_and_github(monkeypatch):
    monkeypatch.setattr(upload.subprocess, "check_output", lambda args, **kw: "ok\n")
    assert upload.command("a") == "ok"
    monkeypatch.setattr(upload, "command", lambda *args: json.dumps({"id": 1}))
    assert upload.github("actions") == {"id": 1}
    monkeypatch.setattr(upload, "command", lambda *args: "160000 commit " + "c" * 40 + " components/AstralKernel")
    assert upload.gitlink("HEAD", "AstralKernel") == "c" * 40
    monkeypatch.setattr(upload, "command", lambda *args: "100644 blob bytes other")
    with pytest.raises(ValueError):
        upload.gitlink("HEAD", "AstralKernel")


def test_download_digest(tmp_path):
    path = tmp_path / "artifact.zip"
    path.write_bytes(b"payload")
    upload.verify_download(path, {"artifact_digest": "sha256:" + hashlib.sha256(b"payload").hexdigest()})
    with pytest.raises(ValueError):
        upload.verify_download(path, {"artifact_digest": "sha256:" + "0" * 64})


def tar(path, members):
    with tarfile.open(path, "w:gz") as archive:
        for name, body, kind in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.mode = 0o755 if kind == tarfile.DIRTYPE else 0o644
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))


def test_restore_tar(tmp_path):
    path = tmp_path / "input.tar.gz"
    tar(path, [("Artifacts", b"", tarfile.DIRTYPE), ("Artifacts/a", b"a", tarfile.REGTYPE)])
    dest = tmp_path / "out"
    dest.mkdir()
    upload.restore_tar(path, dest)
    assert (dest / "Artifacts/a").read_bytes() == b"a"
    with pytest.raises(ValueError, match="overwrite"):
        upload.restore_tar(path, dest)


@pytest.mark.parametrize("name,kind", [("../escape", tarfile.REGTYPE), ("/Artifacts/a", tarfile.REGTYPE),
    ("Artifacts/link", tarfile.SYMTYPE), ("other/a", tarfile.REGTYPE)])
def test_restore_tar_denies_unsafe(tmp_path, name, kind):
    path = tmp_path / "input.tar.gz"
    tar(path, [(name, b"", kind)])
    with pytest.raises(ValueError):
        upload.restore_tar(path, tmp_path / "out")


def test_asc_pagination(monkeypatch):
    urls = []
    def open_url(request, timeout):
        urls.append(request.full_url)
        assert request.get_header("Authorization") == "Bearer token"
        assert timeout == 30
        return io.StringIO(json.dumps({"data": [len(urls)], "links": {"next": upload.API + "next" if len(urls) == 1 else None}}))
    monkeypatch.setattr(upload.urllib.request, "urlopen", open_url)
    assert upload.asc_pages(upload.API + "apps", "token") == [1, 2]
    with pytest.raises(ValueError):
        upload.asc_pages("https://attacker.invalid", "token")
    monkeypatch.setattr(upload.urllib.request, "urlopen", lambda *a, **kw: io.StringIO('{"data":{}}'))
    with pytest.raises(ValueError):
        upload.asc_pages(upload.API + "apps", "token")


@pytest.mark.parametrize("version,build,prior", [("2", "97", []), ("2.0.0", "0", []),
    ("2.0.0", "10000", []), ("2.0.0", "97", ["97"]), ("2.0.0", "97", ["98"]),
    ("2.0.0", "97", ["1.2.3"]), ("2.0.0", "01", [])])
def test_release_identity_denies(version, build, prior):
    with pytest.raises(ValueError):
        upload.release_identity(version, build, [{"attributes": {"version": value}} for value in prior])


def test_preflight(monkeypatch, tmp_path):
    from jose import jwt
    monkeypatch.setattr(jwt, "encode", lambda *a, **kw: "token")
    monkeypatch.setenv("ASC_KEY_ID", "key")
    monkeypatch.setenv("ASC_ISSUER_ID", "issuer")
    key = tmp_path / "key.p8"
    key.write_text("private key is never logged")
    def pages(url, token):
        if "apps?" in url:
            return [{"id": "42", "attributes": {"bundleId": upload.BUNDLE}}]
        assert "IOS" in url and "42" in url
        return [{"attributes": {"version": "96"}}]
    monkeypatch.setattr(upload, "asc_pages", pages)
    assert upload.preflight("2.0.0", "97", key) == {"app_id": "42", "version": "2.0.0", "build": "97", "highest_uploaded_ios_build": 96}
    monkeypatch.setattr(upload, "asc_pages", lambda *a: [])
    with pytest.raises(ValueError):
        upload.preflight("2.0.0", "97", key)
    assert upload.release_identity("2.0.0", "1", [])["build"] == "1"


def profile(bundle, name):
    return {"Name": name, "TeamIdentifier": ["TEAM"], "Platform": ["iOS"],
            "ExpirationDate": datetime.now() + timedelta(days=1),
            "Entitlements": {"application-identifier": "TEAM." + bundle, "get-task-allow": False}}


@pytest.fixture
def profiles(monkeypatch):
    monkeypatch.setenv("APPLE_TEAM_ID", "TEAM")
    monkeypatch.setenv("APPLE_PROFILE_IOS", "phone")
    monkeypatch.setenv("APPLE_PROFILE_WATCH", "watch")
    phone = profile(upload.BUNDLE, "phone")
    watch = profile(upload.BUNDLE + ".watch", "watch")
    def output(args, **kw):
        if args[0] == "security":
            return Path(args[-1]).read_bytes()
        return plistlib.dumps({"application-identifier": "TEAM." + (upload.BUNDLE + ".watch" if args[-1].endswith("AstralWatch.app") else upload.BUNDLE)})
    monkeypatch.setattr(upload.subprocess, "check_output", output)
    monkeypatch.setattr(upload.subprocess, "run", lambda *a, **kw: None)
    return phone, watch


@pytest.mark.parametrize("change", [{"Name": "other"}, {"TeamIdentifier": ["OTHER"]}, {"Platform": []},
    {"Platform": ["macOS"]}, {"ExpirationDate": datetime(2000, 1, 1)}, {"ProvisionedDevices": ["device"]},
    {"ProvisionsAllDevices": True}, {"Entitlements": {"get-task-allow": True}}])
def test_profile_denials(profiles, change):
    value = copy.deepcopy(profiles[0])
    value.update(change)
    with pytest.raises(ValueError):
        upload.profile_identity(value, upload.BUNDLE, "phone", "TEAM")


@pytest.mark.parametrize("container", ["tar", "zip", "raw", "missing", "duplicate"])
def test_install_profiles(profiles, container, tmp_path):
    blob = tmp_path / "profiles"
    entries = [("phone.mobileprovision", plistlib.dumps(profiles[0])),
               ("watch.mobileprovision", plistlib.dumps(profiles[1])),
               ("mac.provisionprofile", plistlib.dumps(profile(upload.BUNDLE, "mac")))]
    if container == "raw":
        blob.write_bytes(entries[0][1])
    elif container == "zip":
        with zipfile.ZipFile(blob, "w") as archive:
            for name, value in entries:
                archive.writestr(name, value)
    else:
        if container == "missing":
            entries = entries[:1]
        if container == "duplicate":
            entries.append(("duplicate.mobileprovision", entries[0][1]))
        tar(blob, [(name, body, tarfile.REGTYPE) for name, body in entries])
    if container in {"raw", "missing", "duplicate"}:
        with pytest.raises(ValueError):
            upload.install_profiles(blob, tmp_path / "installed")
    else:
        upload.install_profiles(blob, tmp_path / "installed")
        assert len(list((tmp_path / "installed").iterdir())) == 2


def make_app(tmp_path, profiles):
    app = tmp_path / "AstralDeep.app"
    watch = app / "Watch/AstralWatch.app"
    watch.mkdir(parents=True)
    for target, bundle, provision in ((app, upload.BUNDLE, profiles[0]), (watch, upload.BUNDLE + ".watch", profiles[1])):
        (target / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": bundle,
            "CFBundleShortVersionString": "2.0.0", "CFBundleVersion": "97"}))
        (target / "embedded.mobileprovision").write_bytes(plistlib.dumps(provision))
    return app


def test_verify_app(profiles, tmp_path):
    app = make_app(tmp_path, profiles)
    upload.verify_app(app, "2.0.0", "97")
    with pytest.raises(ValueError, match="versions disagree"):
        upload.verify_app(app, "2.0.0", "98")
    with pytest.raises(ValueError, match="Watch"):
        upload.verify_app(tmp_path / "missing", "2.0.0", "97")


def test_verify_app_denies_entitlements(profiles, monkeypatch, tmp_path):
    app = make_app(tmp_path, profiles)
    monkeypatch.setattr(upload.subprocess, "check_output", lambda *a, **kw: plistlib.dumps({}))
    with pytest.raises(ValueError, match="entitlements"):
        upload.verify_app(app, "2.0.0", "97")


@pytest.mark.parametrize("action", ["producer", "restore", "preflight", "profiles", "verify-app"])
def test_cli_dispatch(action, monkeypatch, tmp_path):
    from argparse import Namespace
    receipt = tmp_path / "receipt.json"
    receipt.write_text('{"artifact_digest":"value"}')
    args = Namespace(command=action, run_id=11, artifact_id=22, receipt=receipt,
                     input=tmp_path / "input.zip", destination=tmp_path / "dest", version="2.0.0", build="97")
    monkeypatch.setattr(upload.argparse.ArgumentParser, "parse_args", lambda self: args)
    for name in ("producer", "preflight"):
        monkeypatch.setattr(upload, name, lambda *a: {"ok": True})
    for name in ("verify_download", "restore_tar", "install_profiles", "verify_app"):
        monkeypatch.setattr(upload, name, lambda *a: None)
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import extract_release_artifact
    monkeypatch.setattr(extract_release_artifact, "extract_artifact", lambda *a, **kw: None)
    upload.main()
    if action in {"producer", "preflight"}:
        assert json.loads(receipt.read_text()) == {"ok": True}


def workflow_script(fragment):
    import yaml
    jobs = yaml.safe_load((ROOT / ".github/workflows/apple-release.yml").read_text())["jobs"]
    return next(step["run"] for step in jobs["release"]["steps"] if fragment in step.get("run", ""))


@pytest.mark.parametrize("failure", ["none", "archive", "verify-app"])
def test_archive_shell_threads_versions_and_stops_on_failure(tmp_path, failure):
    import os
    import subprocess
    import sys

    binary = tmp_path / "bin"
    binary.mkdir()
    stub = f'#!{sys.executable}\n' + '''import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
with open(os.environ['CALLS'], 'a') as output:
    output.write(json.dumps([name, *sys.argv[1:]]) + '\\n')
args = sys.argv[1:]
if (name == 'xcodebuild' and args[0] == os.environ['FAIL']) or (name == 'python3' and 'verify-app' in args and os.environ['FAIL'] == 'verify-app'):
    sys.exit(7)
if name == 'xcodebuild' and args[0] == '-exportArchive':
    dest = Path(args[args.index('-exportPath') + 1])
    dest.mkdir()
    (dest / 'AstralDeep.ipa').write_bytes(b'verified artifact fixture')
'''
    for name in ("xcodebuild", "ditto", "python3"):
        path = binary / name
        path.write_text(stub)
        path.chmod(0o755)
    (tmp_path / "evidence").mkdir()
    calls = tmp_path / "calls.jsonl"
    (tmp_path / "ExportOptions-ios.plist").write_bytes(plistlib.dumps({"method": "app-store-connect"}))
    environment = {**os.environ, "PATH": str(binary) + ":" + os.environ["PATH"], "CALLS": str(calls),
                   "FAIL": failure, "RUNNER_TEMP": str(tmp_path), "GITHUB_ENV": str(tmp_path / "env"),
                   "PROJECT": "project.xcodeproj", "SCHEME": "AstralApp", "VERSION": "2.0.0", "BUILD": "97",
                   "APPLE_TEAM_ID": "TEAM", "APPLE_PROFILE_IOS": "phone", "APPLE_PROFILE_WATCH": "watch"}
    result = subprocess.run(["bash", "-c", workflow_script("xcodebuild archive")], env=environment,
                            capture_output=True, text=True, check=False)
    commands = [json.loads(line) for line in calls.read_text().splitlines()]
    archive = commands[0]
    assert 'MARKETING_VERSION=2.0.0' in archive and 'CURRENT_PROJECT_VERSION=97' in archive
    assert 'ASTRAL_PROFILE_IOS=phone' in archive and 'ASTRAL_PROFILE_WATCH=watch' in archive
    if failure == "none":
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "evidence/AstralDeep-2.0.0-97.ipa").is_file()
        assert (tmp_path / "evidence/SHA256SUMS").is_file()
    else:
        assert result.returncode != 0
        assert not (tmp_path / "evidence/AstralDeep-2.0.0-97.ipa").exists()


@pytest.mark.parametrize("mode", ["success", "validation-failure", "false-green", "upload-failure"])
def test_upload_shell_requires_real_validation_and_upload_receipts(tmp_path, mode):
    import os
    import subprocess
    import sys

    binary = tmp_path / "bin"
    binary.mkdir()
    calls = tmp_path / "calls"
    stub = f'#!{sys.executable}\n' + '''import os, sys
with open(os.environ['CALLS'], 'a') as output:
    output.write(' '.join(sys.argv[1:]) + '\\n')
validation = '--validate-app' in sys.argv
mode = os.environ['MODE']
if mode == 'validation-failure' and validation:
    print('VERIFY FAILED')
elif mode == 'upload-failure' and not validation:
    print('UPLOAD FAILED')
elif mode != 'false-green':
    print('VERIFY SUCCEEDED' if validation else 'UPLOAD SUCCEEDED')
'''
    path = binary / "xcrun"
    path.write_text(stub)
    path.chmod(0o755)
    (tmp_path / "evidence").mkdir()
    result = subprocess.run(["bash", "-c", workflow_script("--upload-app")], cwd=ROOT,
        env={**os.environ, "PATH": str(binary) + ":" + os.environ["PATH"], "CALLS": str(calls),
             "MODE": mode, "RUNNER_TEMP": str(tmp_path), "IPA": str(tmp_path / "app.ipa"),
             "ASC_KEY_ID": "key", "ASC_ISSUER_ID": "issuer"}, capture_output=True, text=True, check=False)
    assert (result.returncode == 0) is (mode == "success")
    assert (tmp_path / "evidence/validation-receipt.txt").is_file()
    commands = calls.read_text()
    if mode in {"validation-failure", "false-green"}:
        assert '--upload-app' not in commands
    else:
        assert '--upload-app' in commands
        assert (tmp_path / "evidence/upload-receipt.txt").is_file()
