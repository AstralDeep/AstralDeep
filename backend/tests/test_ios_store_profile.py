"""Checks exact-certificate production-push provisioning and bounded Apple API failures.
No tests use Apple services or credential values."""

from __future__ import annotations

import base64
import io
import json
import plistlib
import sys
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import ios_store_profile as signing


@pytest.fixture
def context(monkeypatch, tmp_path):
    for key, value in {'APPLE_TEAM_ID': 'TEAM', 'APPLE_PROFILE_IOS': 'phone', 'APPLE_PROFILE_WATCH': 'watch',
                       'ASC_KEY_ID': 'key', 'ASC_ISSUER_ID': 'issuer', 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(signing, 'decode_profile', lambda payload, directory: plistlib.loads(payload))
    return tmp_path


def profile(push=True, cert=b'certificate', name='phone', watch=False):
    entitlements = {'application-identifier': 'TEAM.' + signing.BUNDLE + ('.watch' if watch else ''),
                    'com.apple.developer.team-identifier': 'TEAM', 'get-task-allow': False}
    if push:
        entitlements['aps-environment'] = 'production'
    return {'Name': name, 'TeamIdentifier': ['TEAM'], 'Platform': ['iOS', 'xrOS', 'visionOS'],
            'ExpirationDate': datetime.now() + timedelta(days=1), 'Entitlements': entitlements,
            'DeveloperCertificates': [cert], 'UUID': 'UUID'}


def item(value, identifier='profile'):
    return {'id': identifier, 'attributes': {'name': value['Name'], 'profileState': 'ACTIVE', 'profileType': 'IOS_APP_STORE',
        'uuid': value['UUID'], 'profileContent': base64.b64encode(plistlib.dumps(value)).decode()}}


class FakeAPI:
    def __init__(self, profiles=(), push=True):
        self.reads = []
        self.writes = []
        self.profiles = list(profiles)
        self.push = push
        self.bundle = {'id': 'bundle', 'attributes': {'identifier': signing.BUNDLE, 'platform': 'IOS', 'seedId': 'TEAM'}}
        self.cert = {'id': 'certificate', 'attributes': {'certificateType': 'DISTRIBUTION',
            'certificateContent': base64.b64encode(b'certificate').decode()}}
        self.created_profile = None

    def probe_profiles(self):
        self.pages('profiles', {'limit': 1})

    def pages(self, path, params=None):
        self.reads.append((path, params))
        if path == 'profiles':
            return []
        if path == 'bundleIds':
            return [self.bundle]
        if path == 'certificates':
            return [self.cert]
        if path.endswith('/bundleIdCapabilities'):
            return [{'id': 'push', 'attributes': {'capabilityType': 'PUSH_NOTIFICATIONS'}}] if self.push else []
        assert path == 'bundleIds/bundle/profiles'
        return self.profiles

    def create(self, path, data):
        self.writes.append((path, data))
        if path == 'bundleIdCapabilities':
            assert data['attributes'] == {'capabilityType': 'PUSH_NOTIFICATIONS'}
            self.push = True
            return {'id': 'push'}
        assert path == 'profiles'
        value = profile(name=data['attributes']['name']) if self.created_profile is None else self.created_profile
        return item(value)


def test_reuse_profile_without_mutations(context):
    api = FakeAPI([item(profile())])
    name, payload, receipt = signing.renewed_profile(api, b'certificate', 'TEAM', context)
    assert name == 'phone' and plistlib.loads(payload)['UUID'] == 'UUID'
    assert receipt['action'] == 'reused'
    assert api.writes == [] and api.reads[0] == ('profiles', {'limit': 1})


@pytest.mark.parametrize('push', [True, False])
def test_create_only_bound_profile_and_missing_push(context, push):
    api = FakeAPI(push=push)
    name, payload, receipt = signing.renewed_profile(api, b'certificate', 'TEAM', context)
    assert receipt['action'] == 'created' and receipt['push_capability_enabled'] is (not push)
    assert plistlib.loads(payload)['Entitlements']['aps-environment'] == 'production'
    assert name.startswith('AstralDeep iOS APNs ')
    assert [path for path, _ in api.writes] == (['profiles'] if push else ['bundleIdCapabilities', 'profiles'])
    request = api.writes[-1][1]
    assert request['attributes']['profileType'] == 'IOS_APP_STORE'
    assert request['relationships'] == {'bundleId': {'data': {'type': 'bundleIds', 'id': 'bundle'}},
        'certificates': {'data': [{'type': 'certificates', 'id': 'certificate'}]}}


def test_skip_other_and_expired_profiles(context):
    expired = profile()
    expired['ExpirationDate'] = datetime(2000, 1, 1)
    inactive = item(profile())
    inactive['attributes']['profileState'] = 'INVALID'
    api = FakeAPI([inactive, item(profile(cert=b'other')), item(expired), item(profile(push=False))])
    assert signing.renewed_profile(api, b'certificate', 'TEAM', context)[2]['action'] == 'created'


@pytest.mark.parametrize('change', ['bundle', 'certificate', 'created-apns', 'created-type', 'created-name', 'uuid', 'run-id', 'capability'])
def test_provisioning_refuses_wrong_bindings(context, monkeypatch, change):
    api = FakeAPI()
    if change == 'bundle':
        api.bundle['attributes']['identifier'] = 'unrelated.bundle'
    elif change == 'certificate':
        api.cert['attributes']['certificateContent'] = base64.b64encode(b'other').decode()
    elif change == 'run-id':
        monkeypatch.setenv('GITHUB_RUN_ID', 'invalid')
    elif change == 'capability':
        api.push = False
        original = api.create
        def create(path, data):
            result = original(path, data)
            if path == 'bundleIdCapabilities':
                api.push = False
            return result
        api.create = create
    else:
        original = api.create
        def create(path, data):
            result = original(path, data)
            value = plistlib.loads(base64.b64decode(result['attributes']['profileContent']))
            if change == 'created-apns':
                value['Entitlements'].pop('aps-environment')
            elif change == 'created-type':
                result['attributes']['profileType'] = 'IOS_APP_DEVELOPMENT'
            elif change == 'created-name':
                result['attributes']['name'] = 'other'
            else:
                result['attributes']['uuid'] = 'other'
            result['attributes']['profileContent'] = base64.b64encode(plistlib.dumps(value)).decode()
            return result
        api.create = create
    with pytest.raises(ValueError):
        signing.renewed_profile(api, b'certificate', 'TEAM', context)
    assert not any(path == 'certificates' for path, _ in api.writes)


@pytest.mark.parametrize('change', ['name', 'team', 'certificate', 'platform', 'expiry', 'development', 'team-entitlement'])
def test_bound_profile_denials(change):
    value = profile()
    if change == 'name': value['Name'] = 'invalid\nname'
    elif change == 'team': value['TeamIdentifier'] = ['OTHER']
    elif change == 'certificate': value['DeveloperCertificates'] = [b'other']
    elif change == 'platform': value['Platform'] = ['watchOS']
    elif change == 'expiry': value['ExpirationDate'] = datetime(2000, 1, 1)
    elif change == 'development': value['Entitlements']['get-task-allow'] = True
    else: value['Entitlements']['com.apple.developer.team-identifier'] = 'OTHER'
    with pytest.raises(ValueError):
        signing.bound_profile(value, value['Name'], 'TEAM', b'certificate')


@pytest.mark.parametrize('renew', [False, True])
def test_prepare_preserves_watch_and_writes_only_validated_phone(context, monkeypatch, renew):
    phone = context / 'astral-ios-0.mobileprovision'
    watch = context / 'astral-ios-1.mobileprovision'
    phone.write_bytes(plistlib.dumps(profile(push=not renew)))
    watch.write_bytes(plistlib.dumps(profile(name='watch', watch=True)))
    before = watch.read_bytes()
    monkeypatch.setattr(signing, 'signing_certificate', lambda *args: b'certificate')
    api = FakeAPI()
    monkeypatch.setattr(signing, 'AppleAPI', lambda *args: api)
    signing.prepare(context/'keychain', context, context/'key', context/'receipt', context/'name')
    assert watch.read_bytes() == before
    assert plistlib.loads(phone.read_bytes())['Entitlements']['aps-environment'] == 'production'
    receipt = json.loads((context/'receipt').read_text())
    assert receipt['action'] == ('created' if renew else 'existing')
    assert receipt['production_apns'] and receipt['exact_distribution_certificate']
    assert api.writes or not renew
    assert not list(context.glob('astral-profile-prepared-*'))


def test_prepare_denies_ambiguity_and_api_failure_without_touching_profiles(context, monkeypatch):
    monkeypatch.setattr(signing, 'signing_certificate', lambda *args: b'certificate')
    with pytest.raises(ValueError):
        signing.prepare(context/'keychain', context, context/'key', context/'receipt', context/'name')
    phone = context/'astral-ios-0.mobileprovision'
    watch = context/'astral-ios-1.mobileprovision'
    phone.write_bytes(plistlib.dumps(profile(push=False)))
    watch.write_bytes(plistlib.dumps(profile(name='watch', watch=True)))
    before = [phone.read_bytes(), watch.read_bytes()]
    def denied(*args):
        raise signing.ProvisioningError('Apple API HTTP 403')
    monkeypatch.setattr(signing, 'AppleAPI', denied)
    with pytest.raises(signing.ProvisioningError):
        signing.prepare(context/'keychain', context, context/'key', context/'receipt', context/'name')
    assert [phone.read_bytes(), watch.read_bytes()] == before
    assert not (context/'receipt').exists() and not (context/'name').exists()


@pytest.fixture
def api(context, monkeypatch):
    from jose import jwt
    monkeypatch.setattr(jwt, 'encode', lambda *args, **kwargs: 'private-token')
    key = context/'key'
    key.write_text('private-key')
    return signing.AppleAPI(key)


def test_api_get_post_and_pagination(api):
    requests = []
    def open_request(request, timeout):
        requests.append(request)
        assert timeout == 30
        return io.BytesIO(json.dumps({'data': [], 'links': {}}).encode())
    api.opener = SimpleNamespace(open=open_request)
    api.probe_profiles()
    requests.clear()
    assert api.pages('profiles', {'limit': 1}) == []
    assert api.create('bundleIdCapabilities', {'type': 'bundleIdCapabilities',
        'attributes': {'capabilityType': 'PUSH_NOTIFICATIONS'}, 'relationships': {'bundleId': {'data': {'type': 'bundleIds', 'id': 'bound'}}}}) == []
    assert requests[0].get_method() == 'GET' and requests[1].get_method() == 'POST'
    api.request = lambda url: {'data': [], 'links': {'next': signing.API+'profiles'}}
    with pytest.raises(ValueError, match='pagination'):
        api.pages('profiles')


@pytest.mark.parametrize('code,body', [(403, b'{"errors":[{"code":"FORBIDDEN.REQUIRED_AGREEMENTS_MISSING","detail":"private"}]}'),
    (403, b'not-json'), (403, b'{"errors":[{"code":"private identifier"}]}'), (401, b'private')])
def test_api_errors_are_honest_and_do_not_echo_private_data(api, code, body):
    def denied(*args, **kwargs):
        raise urllib.error.HTTPError(signing.API+'profiles', code, 'private', {}, io.BytesIO(body))
    api.opener = SimpleNamespace(open=denied)
    with pytest.raises(signing.ProvisioningError) as error:
        api.pages('profiles', {'limit': 1})
    assert str(code) in str(error.value) and 'private' not in str(error.value)
    if code == 403:
        assert 'agreements' in str(error.value) and 'Profiles access' in str(error.value)


def test_api_bounds_and_mutation_denials(api):
    for url, payload in [('https://untrusted.invalid', None), (signing.API+'certificates', {'data': {}}),
        (signing.API+'profiles', {'data': {'type': 'profiles','attributes': {'profileType':'IOS_APP_DEVELOPMENT'}}}),
        (signing.API+'bundleIdCapabilities', {'data': {'type':'bundleIdCapabilities', 'attributes': {'capabilityType':'ICLOUD'}}})]:
        with pytest.raises(ValueError):
            api.request(url, payload)
    api.opener = SimpleNamespace(open=lambda *args, **kw: io.BytesIO(b'x'*(8*1024**2+1)))
    with pytest.raises(ValueError, match='size'):
        api.request(signing.API+'profiles')
    api.opener = SimpleNamespace(open=lambda *args, **kw: io.BytesIO(b'{}'))
    with pytest.raises(ValueError, match='invalid'):
        api.request(signing.API+'profiles')
    api.request = lambda *args: {'data': {}}
    with pytest.raises(ValueError):
        api.pages('profiles')
    with pytest.raises(ValueError):
        signing.resource_id('../other')
    with pytest.raises(signing.ProvisioningError):
        signing.NoRedirect().redirect_request(None,None,302,None,None,'https://untrusted.invalid')


def test_decode_profile_cleans_temporary_file(context, monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(signing.subprocess, 'check_output', lambda args, **kw: plistlib.dumps({'decoded': True}))
    assert signing.decode_profile(b'CMS', context) == {'decoded':True}
    assert not list(context.iterdir())
    with pytest.raises(ValueError):
        signing.decode_profile(b'', context)


def test_signing_certificate_exact_identity(context, monkeypatch):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Apple Distribution: Test'),
                        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME,'TEAM')])
    cert = x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key()).serial_number(1).not_valid_before(datetime.now(UTC)-timedelta(days=1)).not_valid_after(datetime.now(UTC)+timedelta(days=1)).sign(key,hashes.SHA256())
    identity = cert.fingerprint(hashes.SHA1()).hex().upper()
    pem = cert.public_bytes(serialization.Encoding.PEM)
    monkeypatch.setattr(signing.subprocess,'check_output',lambda args,**kw: f'1) {identity} "Apple Distribution: Test"' if args[1]=='find-identity' else pem)
    assert signing.signing_certificate(context/'keychain','TEAM') == cert.public_bytes(serialization.Encoding.DER)
    with pytest.raises(ValueError):
        signing.signing_certificate(context/'keychain','OTHER')
    monkeypatch.setattr(signing.subprocess,'check_output',lambda args,**kw: '' if args[1]=='find-identity' else pem)
    with pytest.raises(ValueError):
        signing.signing_certificate(context/'keychain','TEAM')


@pytest.mark.parametrize('denied', [False,True])
def test_cli_dispatch(context, monkeypatch, denied):
    args = SimpleNamespace(keychain=context/'keychain',profiles_dir=context,key=context/'key',receipt=context/'receipt',name_file=context/'name')
    monkeypatch.setattr(signing.argparse.ArgumentParser,'parse_args',lambda self:args)
    def prepare(*args):
        if denied: raise signing.ProvisioningError('permission denied')
    monkeypatch.setattr(signing,'prepare',prepare)
    assert signing.main() == (1 if denied else 0)


def test_read_only_access_probe_precedes_every_possible_mutation(context):
    api = FakeAPI(push=False)
    def forbidden(path, params=None):
        assert path == 'profiles' and params == {'limit': 1}
        raise signing.ProvisioningError('Apple API HTTP 403')
    api.pages = forbidden
    with pytest.raises(signing.ProvisioningError):
        signing.renewed_profile(api, b'certificate', 'TEAM', context)
    assert api.writes == []


def test_api_never_forwards_authorization_to_foreign_pagination_target(api):
    requests = []
    def opener(request, timeout):
        requests.append(request)
        return io.BytesIO(json.dumps({'data': [], 'links': {'next': 'https://untrusted.invalid/profiles'}}).encode())
    api.opener = SimpleNamespace(open=opener)
    with pytest.raises(ValueError, match='origin'):
        api.pages('profiles')
    assert len(requests) == 1
    assert requests[0].full_url.startswith(signing.API)


def test_api_retains_only_public_error_fields(api, capsys):
    body = {'errors': [{'code':'FORBIDDEN.REQUIRED_AGREEMENTS_MISSING',
                        'title':'Access Forbidden','detail':'A required agreement is missing or has expired.'}]}
    def denied(*args, **kwargs):
        raise urllib.error.HTTPError(signing.API+'profiles',403,'private',{},io.BytesIO(json.dumps(body).encode()))
    api.opener = SimpleNamespace(open=denied)
    with pytest.raises(signing.ProvisioningError):
        api.pages('profiles')
    report = json.loads(capsys.readouterr().out)['apple_provisioning_error']
    assert report == {'status':403,'code':'FORBIDDEN.REQUIRED_AGREEMENTS_MISSING',
                      'title':'Access Forbidden','detail':'A required agreement is missing or has expired.'}


def test_workflow_uses_verified_profile_name_for_archive_and_export():
    workflow = (ROOT / ".github/workflows/apple-release.yml").read_text()
    prepare_step = workflow.index("- name: Prepare exact-certificate production push profile for iPhone")
    archive_step = workflow.index("- name: Archive and export iPhone with embedded Watch")
    assert prepare_step < archive_step
    assert "python3 scripts/ios_store_profile.py" in workflow[prepare_step:archive_step]
    assert 'IOS_RELEASE_PROFILE_NAME=%s' in workflow[prepare_step:archive_step]
    assert "APPLE_PROFILE_IOS: ${{ env.IOS_RELEASE_PROFILE_NAME }}" in workflow[archive_step:]
    assert "APPLE_PROFILE_WATCH: ${{ secrets.APPLE_PROFILE_WATCH }}" in workflow[archive_step:]
    assert '"$RUNNER_TEMP/ios-profile-name.txt"' in workflow[workflow.index("- name: Remove signing material"):]
