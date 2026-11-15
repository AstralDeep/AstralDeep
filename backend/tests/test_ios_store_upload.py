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
        info = {"CFBundleIdentifier": bundle, "CFBundleShortVersionString": "2.0.0", "CFBundleVersion": "97"}
        if target == app:
            info["NSFaceIDUsageDescription"] = "Verify approval of mesh changes and sensitive actions."
        (target / "Info.plist").write_bytes(plistlib.dumps(info))
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


@pytest.mark.parametrize("purpose", [None, "", " \n ", 0, [], {}])
def test_verify_app_denies_absent_or_invalid_face_id_purpose(profiles, tmp_path, purpose):
    app = make_app(tmp_path, profiles)
    path = app / "Info.plist"
    info = plistlib.loads(path.read_bytes())
    if purpose is None:
        del info["NSFaceIDUsageDescription"]
    else:
        info["NSFaceIDUsageDescription"] = purpose
    path.write_bytes(plistlib.dumps(info))
    with pytest.raises(ValueError, match="Face ID"):
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


def apple_double(entries=None):
    import struct

    records = [(9, b'\0' * 32)] if entries is None else entries
    offset = 26 + 12 * len(records)
    descriptors = []
    payloads = []
    for identifier, payload in records:
        descriptors.append(struct.pack('>III', identifier, offset, len(payload)))
        payloads.append(payload)
        offset += len(payload)
    return struct.pack('>II16sH', 0x00051607, 0x00020000, b'Mac OS X        ', len(records)) + b''.join(descriptors + payloads)


def profile_container(blob, kind, entries):
    if kind == 'tar':
        tar(blob, [(name, body, tarfile.REGTYPE) for name, body in entries])
    else:
        with zipfile.ZipFile(blob, 'w') as archive:
            for name, body in entries:
                archive.writestr(name, body)


@pytest.mark.parametrize('kind', ['tar', 'zip'])
def test_install_profiles_accepts_valid_appledouble_only(profiles, tmp_path, capsys, kind):
    blob = tmp_path / 'profiles'
    entries = [('._phone.mobileprovision', apple_double()),
               ('phone.mobileprovision', plistlib.dumps(profiles[0])),
               ('__MACOSX/._watch.mobileprovision', apple_double([(9, b'\0' * 32), (2, b'')])),
               ('watch.mobileprovision', plistlib.dumps(profiles[1]))]
    profile_container(blob, kind, entries)
    upload.install_profiles(blob, tmp_path / 'installed')
    assert len(list((tmp_path / 'installed').iterdir())) == 2
    diagnostics = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    summary = next(item for item in diagnostics if "appledouble_skipped" in item)
    assert summary['container'] == kind and summary['entries'] == 4
    assert summary['appledouble_skipped'] == 2 and summary['profile_payloads'] == 2
    assert all(name not in json.dumps(diagnostics) for name, _ in entries)
    assert 'TEAM' not in json.dumps(diagnostics)


@pytest.mark.parametrize('kind', ['tar', 'zip'])
@pytest.mark.parametrize('name,body', [('._phone.mobileprovision', b'not metadata'),
    ('real.mobileprovision', b'not a real profile'),
    ('real.mobileprovision', apple_double())])
def test_profile_container_refuses_invalid_sidecars_and_corrupt_profiles(profiles, tmp_path, kind, name, body):
    blob = tmp_path / 'profiles'
    profile_container(blob, kind, [(name, body), ('phone.mobileprovision', plistlib.dumps(profiles[0])),
                                ('watch.mobileprovision', plistlib.dumps(profiles[1]))])
    with pytest.raises((ValueError, plistlib.InvalidFileException)):
        upload.install_profiles(blob, tmp_path / 'installed')
    assert not (tmp_path / 'installed').exists()


@pytest.mark.parametrize('kind', ['tar', 'zip'])
def test_profile_container_sidecar_does_not_hide_ambiguous_profiles(profiles, tmp_path, kind):
    blob = tmp_path / 'profiles'
    profile_container(blob, kind, [('._phone.mobileprovision', apple_double()),
        ('phone.mobileprovision', plistlib.dumps(profiles[0])), ('second.mobileprovision', plistlib.dumps(profiles[0])),
        ('watch.mobileprovision', plistlib.dumps(profiles[1]))])
    with pytest.raises(ValueError, match='ambiguous'):
        upload.install_profiles(blob, tmp_path / 'installed')


@pytest.mark.parametrize('mutation', ['short', 'magic', 'version', 'zero-count', 'truncated-table',
    'data-fork', 'duplicate-id', 'inside-header', 'past-end', 'overlap'])
def test_appledouble_refuses_malformed_header_and_bounds(mutation):
    import struct

    payload = bytearray(apple_double([(9, b'\0' * 32), (2, b'resource')]))
    if mutation == 'short':
        payload = payload[:10]
    elif mutation == 'magic':
        struct.pack_into('>I', payload, 0, 0x00051600)
    elif mutation == 'version':
        struct.pack_into('>I', payload, 4, 0x00010000)
    elif mutation == 'zero-count':
        struct.pack_into('>H', payload, 24, 0)
    elif mutation == 'truncated-table':
        struct.pack_into('>H', payload, 24, 50)
    elif mutation == 'data-fork':
        struct.pack_into('>I', payload, 26, 1)
    elif mutation == 'duplicate-id':
        struct.pack_into('>I', payload, 38, 9)
    elif mutation == 'inside-header':
        struct.pack_into('>I', payload, 30, 26)
    elif mutation == 'past-end':
        struct.pack_into('>I', payload, 34, len(payload) + 1)
    else:
        struct.pack_into('>I', payload, 42, 50)
    with pytest.raises(ValueError):
        upload.apple_double_metadata('._profile.mobileprovision', bytes(payload))


@pytest.mark.parametrize('name', ['profile.mobileprovision', '../._profile.mobileprovision',
    '/._profile.mobileprovision', 'folder\\._profile.mobileprovision', '._'])
def test_appledouble_requires_conventional_metadata_name(name):
    assert not upload.apple_double_metadata(name, apple_double())


@pytest.mark.parametrize('change,failed,known,unknown', [
    ({'Name': 'private mismatched name'}, 'name_matches', ['iOS'], False),
    ({'TeamIdentifier': ['private mismatched team']}, 'team_matches', ['iOS'], False),
    ({'Entitlements': {'application-identifier': 'private mismatched app'}}, 'app_id_matches', ['iOS'], False),
    ({'Platform': ['macOS']}, 'platform_tags_allowed', ['macOS'], False),
    ({'Platform': []}, 'platform_nonempty', [], False),
    ({'Platform': ['visionOS']}, 'platform_tags_allowed', ['visionOS'], False),
    ({'Platform': ['private platform marker']}, 'platform_tags_allowed', [], True),
])
def test_profile_identity_reports_only_boolean_checks_and_public_tags(profiles, capsys, change, failed, known, unknown):
    value = copy.deepcopy(profiles[0])
    value.update(change)
    with pytest.raises(ValueError, match=failed):
        upload.profile_identity(value, upload.BUNDLE, 'phone', 'TEAM')
    output = capsys.readouterr().out
    diagnostics = json.loads(output)
    assert {key for key, passed in diagnostics['profile_identity'].items() if not passed} == {failed}
    assert all(type(passed) is bool for passed in diagnostics['profile_identity'].values())
    assert diagnostics['known_platform_tags'] == known
    assert diagnostics['unknown_platform_tags_present'] is unknown
    for sensitive in ('private', 'TEAM', upload.BUNDLE, 'phone'):
        assert sensitive not in output


@pytest.mark.parametrize('target', [0, 1])
def test_profile_accepts_observed_ios_family_platforms(profiles, target):
    value = copy.deepcopy(profiles[target])
    value['Platform'] = ['iOS', 'xrOS', 'visionOS']
    bundle = upload.BUNDLE + ('.watch' if target else '')
    upload.profile_identity(value, bundle, 'watch' if target else 'phone', 'TEAM')


@pytest.mark.parametrize('platforms', [['iOS'], ['watchOS'], ['iOS', 'watchOS'], ['iOS', 'xrOS'], ['iOS', 'visionOS']])
@pytest.mark.parametrize('target', [0, 1])
def test_profile_platform_allowances_preserve_matching_identity(profiles, platforms, target):
    value = copy.deepcopy(profiles[target])
    value['Platform'] = platforms
    bundle = upload.BUNDLE + ('.watch' if target else '')
    upload.profile_identity(value, bundle, 'watch' if target else 'phone', 'TEAM')
    with pytest.raises(ValueError, match='app_id_matches'):
        upload.profile_identity(value, upload.BUNDLE + '.wrong', 'watch' if target else 'phone', 'TEAM')
    with pytest.raises(ValueError, match='team_matches'):
        upload.profile_identity(value, bundle, 'watch' if target else 'phone', 'OTHER')


@pytest.mark.parametrize('platforms', [['visionOS'], ['xrOS'], ['xrOS', 'visionOS'], ['macOS'], ['tvOS'],
    ['iOS', 'macOS'], ['iOS', 'tvOS'], ['iOS', 'unknown'], ['watchOS', 'visionOS'], ['iOS', 'watchOS', 'xrOS']])
@pytest.mark.parametrize('target', [0, 1])
def test_profile_platform_family_rejects_other_or_unanchored_platforms(profiles, platforms, target):
    value = copy.deepcopy(profiles[target])
    value['Platform'] = platforms
    with pytest.raises(ValueError, match='platform_tags_allowed'):
        upload.profile_identity(value, upload.BUNDLE + ('.watch' if target else ''), 'watch' if target else 'phone', 'TEAM')


COMPAT_INFO = 'apple-clients/AstralApp/Info.plist'
COMPAT_TESTS = 'apple-clients/AstralApp/AstralAppTests/MeshConsentTests.swift'
COMPAT_LEDGER = 'provenance/transformations.json'
COMPAT_ANCHOR = 'final class LocalAuthenticatorTests: XCTestCase {\n'
COMPAT_ADDITION = '''    func testAppBundleDeclaresFaceIDPurposeForBiometricConsent() throws {
        let purpose = try XCTUnwrap(
            Bundle(for: AppModel.self).object(forInfoDictionaryKey: "NSFaceIDUsageDescription") as? String)
        XCTAssertFalse(purpose.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
    }

'''


def compat_git(repo, *args):
    import subprocess

    result = subprocess.run(['git', '-C', str(repo), '-c', 'commit.gpgsign=false',
        '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', *args],
        capture_output=True, check=True)
    return result.stdout.decode().strip()


@pytest.fixture
def projection_repo(tmp_path, monkeypatch):
    repo = tmp_path / 'projection'
    repo.mkdir()
    compat_git(repo, 'init', '-q')
    compat_git(repo, 'config', 'core.filemode', 'true')
    info = repo / COMPAT_INFO
    tests = repo / COMPAT_TESTS
    info.parent.mkdir(parents=True)
    tests.parent.mkdir(parents=True)
    old_info = {'CFBundleName': 'AstralDeep', 'LSRequiresIPhoneOS': True}
    old_test = 'import XCTest\n' + COMPAT_ANCHOR + '    func testExistingConsentDenial() {}\n}\n'
    info.write_bytes(plistlib.dumps(old_info))
    tests.write_text(old_test)
    ledger = repo / COMPAT_LEDGER
    ledger.parent.mkdir(parents=True)
    old_ledger = {'format':'astral.extraction-transformations/v1', 'sourceManifestSha256':'a' * 64,
                  'entries':[{'path':COMPAT_INFO, 'sourceBlob':'b' * 40, 'sourceTuple':['origin', 1],
                              'reason':'Preserve the source binding',
                              'resultSha256':hashlib.sha256(info.read_bytes()).hexdigest()},
                             {'path':'other-file', 'resultSha256':'c' * 64, 'active':True}]}
    ledger.write_text(json.dumps(old_ledger, indent=2))
    compat_git(repo, 'add', '.')
    compat_git(repo, 'commit', '-qm', 'Original')
    old = compat_git(repo, 'rev-parse', 'HEAD')
    new_info = {**old_info, 'NSFaceIDUsageDescription': 'Verify protected actions.'}
    info.write_bytes(plistlib.dumps(new_info))
    tests.write_text(old_test.replace(COMPAT_ANCHOR, COMPAT_ANCHOR + COMPAT_ADDITION))
    new_ledger = copy.deepcopy(old_ledger)
    new_ledger['entries'][0]['resultSha256'] = hashlib.sha256(info.read_bytes()).hexdigest()
    ledger.write_text(json.dumps(new_ledger, indent=2))
    monkeypatch.chdir(repo)
    return repo, old, old_info, old_test


def compat_commit(repo):
    compat_git(repo, 'add', '-A')
    compat_git(repo, 'commit', '--allow-empty', '-qm', 'Consumer')
    return compat_git(repo, 'rev-parse', 'HEAD')


def test_projection_purpose_compatibility_uses_real_git_tree(projection_repo):
    repo, old, _, _ = projection_repo
    new = compat_commit(repo)
    proof = upload.projection_compatibility(old, new)
    assert proof['mode'] == 'face-id-purpose-addition'
    assert proof['original_projection'] == old and proof['current_projection'] == new
    assert proof['changed_paths'] == [COMPAT_TESTS, COMPAT_INFO, COMPAT_LEDGER]
    assert proof['ancestor'] is True
    assert proof['info_only_adds_face_id_purpose'] is True
    assert proof['tests_exact_host_bundle_regression'] is True
    assert proof['provenance_only_updates_info_result'] is True
    assert upload.projection_compatibility(old, old)['mode'] == 'exact'


@pytest.mark.parametrize('mutation', ['outside', 'newline-path', 'gitlink', 'info-executable', 'test-executable',
    'info-symlink', 'test-symlink', 'deleted', 'renamed', 'no-delta', 'info-other-key', 'info-bool-to-int',
    'purpose-missing', 'purpose-empty', 'purpose-whitespace', 'purpose-number', 'purpose-list', 'purpose-dict',
    'info-invalid', 'info-duplicate-key', 'test-arbitrary-addition', 'test-existing-change', 'test-wrong-bundle'])
def test_projection_compatibility_denies_other_tree_or_semantic_changes(projection_repo, mutation):
    repo, old, old_info, old_test = projection_repo
    info = repo / COMPAT_INFO
    tests = repo / COMPAT_TESTS
    new_info = {**old_info, 'NSFaceIDUsageDescription': 'Verify protected actions.'}
    if mutation in {'outside', 'newline-path'}:
        path = repo / ('unapproved.swift' if mutation == 'outside' else 'unapproved\nfile.swift')
        path.write_text('unapproved')
    elif mutation == 'gitlink':
        compat_git(repo, 'config', 'diff.ignoreSubmodules', 'all')
        compat_git(repo, 'add', COMPAT_INFO, COMPAT_TESTS, COMPAT_LEDGER)
        compat_git(repo, 'update-index', '--add', '--cacheinfo', '160000,' + old + ',unapproved-component')
    elif mutation in {'info-executable', 'test-executable'}:
        (info if mutation == 'info-executable' else tests).chmod(0o755)
    elif mutation in {'info-symlink', 'test-symlink'}:
        path = info if mutation == 'info-symlink' else tests
        path.unlink()
        path.symlink_to('unapproved-target')
    elif mutation == 'deleted':
        info.unlink()
    elif mutation == 'renamed':
        info.rename(info.with_name('Renamed.plist'))
    elif mutation == 'no-delta':
        info.write_bytes(plistlib.dumps(old_info))
        tests.write_text(old_test)
    elif mutation in {'info-other-key', 'info-bool-to-int'}:
        new_info['CFBundleName' if mutation == 'info-other-key' else 'LSRequiresIPhoneOS'] = 'Changed' if mutation == 'info-other-key' else 1
        info.write_bytes(plistlib.dumps(new_info))
    elif mutation.startswith('purpose-'):
        values = {'purpose-empty':'', 'purpose-whitespace':' \n ', 'purpose-number':1, 'purpose-list':[], 'purpose-dict':{}}
        if mutation == 'purpose-missing':
            info.write_bytes(plistlib.dumps(old_info, fmt=plistlib.FMT_BINARY))
        else:
            new_info['NSFaceIDUsageDescription'] = values[mutation]
            info.write_bytes(plistlib.dumps(new_info))
    elif mutation == 'info-invalid':
        info.write_bytes(b'not a plist')
    elif mutation == 'info-duplicate-key':
        raw = plistlib.dumps(new_info).replace(b'<key>CFBundleName</key>', b'<key>CFBundleName</key><string>Changed</string><key>CFBundleName</key>')
        info.write_bytes(raw)
    elif mutation == 'test-arbitrary-addition':
        tests.write_text(tests.read_text() + 'func arbitraryCode() {}\n')
    elif mutation == 'test-existing-change':
        tests.write_text(tests.read_text().replace('testExistingConsentDenial', 'disabledExistingConsentDenial'))
    else:
        tests.write_text(tests.read_text().replace('Bundle(for: AppModel.self)', 'Bundle.main'))
    if mutation == 'gitlink':
        compat_git(repo, 'commit', '-qm', 'Consumer with gitlink')
        new = compat_git(repo, 'rev-parse', 'HEAD')
    else:
        new = compat_commit(repo)
    with pytest.raises(ValueError):
        upload.projection_compatibility(old, new)


def test_projection_compatibility_requires_ancestor_and_immutable_commit_ids(projection_repo):
    repo, old, _, _ = projection_repo
    compat_git(repo, 'checkout', '--orphan', 'other-history')
    new = compat_commit(repo)
    with pytest.raises(ValueError, match='ancestor'):
        upload.projection_compatibility(old, new)
    for bad in ('main', '0' * 40):
        with pytest.raises(ValueError):
            upload.projection_compatibility(old, bad)


def test_projection_compatibility_rejects_preexisting_purpose(projection_repo):
    repo, _, old_info, old_test = projection_repo
    path = repo / COMPAT_INFO
    path.write_bytes(plistlib.dumps({**old_info, 'NSFaceIDUsageDescription': 'Already present'}))
    (repo / COMPAT_TESTS).write_text(old_test)
    old = compat_commit(repo)
    path.write_bytes(plistlib.dumps({**old_info, 'NSFaceIDUsageDescription': 'Changed'}))
    (repo / COMPAT_TESTS).write_text(old_test.replace(COMPAT_ANCHOR, COMPAT_ANCHOR + COMPAT_ADDITION))
    with pytest.raises(ValueError):
        upload.projection_compatibility(old, compat_commit(repo))


def test_producer_retains_origin_and_requires_current_native_ci_for_compatibility(provider, monkeypatch):
    old, new, kernel = 'd' * 40, 'e' * 40, 'c' * 40
    monkeypatch.setattr(upload, 'gitlink', lambda ref, component: kernel if component == 'AstralKernel' else new if ref == 'HEAD' else old)
    provider[2]['workflow_runs'][0]['head_sha'] = new
    proof = {'mode':'face-id-purpose-addition','original_projection':old,'current_projection':new,
             'changed_paths':[COMPAT_TESTS, COMPAT_INFO, COMPAT_LEDGER]}
    monkeypatch.setattr(upload, 'projection_compatibility', lambda a, b: proof, raising=False)
    calls = []
    monkeypatch.setattr(upload.subprocess, 'run', lambda args, **kwargs: calls.append(args))
    receipt = upload.producer(11, 22)
    assert receipt['original_projection'] == old and receipt['current_projection'] == new
    assert receipt['projection_compatibility'] == proof
    assert any('https://github.com/AstralDeep/AstralProjection.git' in args and old in args and new in args for args in calls)
    provider[2]['workflow_runs'][0]['head_sha'] = old
    with pytest.raises(ValueError, match='Projection'):
        upload.producer(11, 22)


@pytest.mark.parametrize('mutation', ['hash', 'hash-type', 'source-blob', 'source-tuple', 'reason', 'source-manifest',
    'other-entry', 'other-entry-bool-type', 'top-level', 'added-entry', 'removed-entry', 'reordered-entries',
    'duplicate-info-entry', 'wrong-info-path', 'duplicate-json-key', 'duplicate-entry-key', 'invalid-json',
    'non-finite-json', 'ledger-unchanged', 'ledger-executable', 'ledger-deleted', 'root-array', 'entries-dict', 'entry-array'])
def test_projection_ledger_allows_only_exact_info_result_binding(projection_repo, mutation):
    repo, old, _, _ = projection_repo
    path = repo / COMPAT_LEDGER
    ledger = json.loads(path.read_text())
    target = ledger['entries'][0]
    if mutation == 'hash':
        target['resultSha256'] = '0' * 64
    elif mutation == 'hash-type':
        target['resultSha256'] = 123
    elif mutation == 'source-blob':
        target['sourceBlob'] = '0' * 40
    elif mutation == 'source-tuple':
        target['sourceTuple'] = ['changed', 1]
    elif mutation == 'reason':
        target['reason'] = 'Changed'
    elif mutation == 'source-manifest':
        ledger['sourceManifestSha256'] = '0' * 64
    elif mutation == 'other-entry':
        ledger['entries'][1]['resultSha256'] = '0' * 64
    elif mutation == 'other-entry-bool-type':
        ledger['entries'][1]['active'] = 1
    elif mutation == 'top-level':
        ledger['new-field'] = True
    elif mutation == 'added-entry':
        ledger['entries'].append({'path':'new-file'})
    elif mutation == 'removed-entry':
        ledger['entries'].pop()
    elif mutation == 'reordered-entries':
        ledger['entries'].reverse()
    elif mutation == 'duplicate-info-entry':
        ledger['entries'].append(copy.deepcopy(target))
    elif mutation == 'wrong-info-path':
        target['path'] = 'not-the-info-path'
    elif mutation == 'root-array':
        ledger = [ledger]
    elif mutation == 'entries-dict':
        ledger['entries'] = {}
    elif mutation == 'entry-array':
        ledger['entries'][1] = []
    if mutation == 'duplicate-json-key':
        path.write_text(json.dumps(ledger).replace('"format":', '"format":"ignored", "format":', 1))
    elif mutation == 'duplicate-entry-key':
        path.write_text(json.dumps(ledger).replace('"resultSha256":', '"resultSha256":"ignored", "resultSha256":', 1))
    elif mutation == 'invalid-json':
        path.write_text('{broken')
    elif mutation == 'non-finite-json':
        path.write_text(json.dumps(ledger).replace('true', 'NaN', 1))
    elif mutation == 'ledger-unchanged':
        path.write_text(compat_git(repo, 'show', old + ':' + COMPAT_LEDGER))
    elif mutation == 'ledger-executable':
        path.chmod(0o755)
    elif mutation == 'ledger-deleted':
        path.unlink()
    else:
        path.write_text(json.dumps(ledger, indent=2))
    with pytest.raises(ValueError):
        upload.projection_compatibility(old, compat_commit(repo))


def test_projection_ledger_accepts_formatting_without_changing_other_values(projection_repo):
    repo, old, _, _ = projection_repo
    path = repo / COMPAT_LEDGER
    path.write_text(json.dumps(json.loads(path.read_text()), sort_keys=True, separators=(',', ':')))
    proof = upload.projection_compatibility(old, compat_commit(repo))
    assert proof['provenance_only_updates_info_result'] is True


def test_projection_ledger_rejects_unbound_original_info_result(projection_repo):
    repo, old, _, _ = projection_repo
    info = (repo / COMPAT_INFO).read_bytes()
    tests = (repo / COMPAT_TESTS).read_bytes()
    current_ledger = json.loads((repo / COMPAT_LEDGER).read_text())
    compat_git(repo, 'reset', '--hard', old)
    path = repo / COMPAT_LEDGER
    original = json.loads(path.read_text())
    original['entries'][0]['resultSha256'] = '0' * 64
    path.write_text(json.dumps(original))
    old = compat_commit(repo)
    (repo / COMPAT_INFO).write_bytes(info)
    (repo / COMPAT_TESTS).write_bytes(tests)
    path.write_text(json.dumps(current_ledger))
    with pytest.raises(ValueError, match='hash|binding|result'):
        upload.projection_compatibility(old, compat_commit(repo))
