"""Covers the content-free wake gateway: the APNs background push carries only a wake flag, per-device rate limits hold, destinations and providers fail closed, and the trigger wakes a device that is not connected exactly when the node holds new events for it, through a fake APNs transport."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from shared.external_http import AuthFailedError, BadRequestError, ServiceUnreachableError

from orchestrator import institution_runtime, node_mesh, node_mesh_chat as nmc, push_wake as pw
from orchestrator.institution_meshes import Owner
from orchestrator.push_providers import ApnsPushAdapter, PushProviderError
from shared import institution_profile as ip
from shared import node_identity as ni
from tests import fake_astral_kernel as fake
from tests.test_node_mesh_chat import History, StubMailbox, answering, no_issuers, settle

PHONE = "sha256:" + "ab" * 32
HEX = "ab" * 32
OWNER = "alice"


class FakeTransport:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def __call__(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return SimpleNamespace(status_code=200, text="")


def pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


def adapter(transport, *, topic="com.example.app"):
    return ApnsPushAdapter(key_id="KEYID", team_id="TEAM", private_key=pem(), topic=topic, transport=transport, clock=lambda: 1000.0)


class Targets:
    def __init__(self, record=None, error=None):
        self.record = record
        self.error = error
        self.asked = []

    def target(self, owner_id, mesh_device_id):
        self.asked.append((owner_id, mesh_device_id))
        if self.error:
            raise self.error
        return self.record


APNS = {"provider": "apns", "push_token": "device-token-1"}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def gateway(transport=None, targets=None, *, limiter=None, apns=None):
    transport = transport or FakeTransport()
    return pw.WakeGateway(
        targets=targets or Targets(APNS), adapters={"apns": apns or adapter(transport)}, limiter=limiter or pw.WakeLimiter(min_interval_seconds=0)
    ), transport


def test_the_apns_wake_is_a_background_push_with_no_content():
    transport = FakeTransport()
    adapter(transport).send_wake(device_token="tok")
    call = transport.calls[0]
    assert call["url"] == "https://api.push.apple.com/3/device/tok" and call["method"] == "POST"
    assert call["json_body"] == {"aps": {"content-available": 1}}
    headers = call["extra_headers"]
    assert headers["apns-push-type"] == "background" and headers["apns-priority"] == "5"
    assert headers["apns-topic"] == "com.example.app" and headers["apns-collapse-id"] == "astral-wake"
    assert headers["apns-expiration"] == str(1000 + 3600)
    assert call["api_key"]


def test_an_unconfigured_apns_adapter_refuses_the_wake_as_transient():
    with pytest.raises(PushProviderError) as info:
        ApnsPushAdapter(key_id="", team_id="", private_key="", topic="").send_wake(device_token="t")
    assert (info.value.code, info.value.transient) == ("provider_unconfigured", True)


@pytest.mark.parametrize(
    ("error", "code", "transient"),
    [
        (BadRequestError("Upstream returned 410: {\"reason\": \"Unregistered\"}"), "token_unregistered", False),
        (AuthFailedError("Authentication failed (403)"), "provider_credentials_rejected", True),
        (ServiceUnreachableError("timeout"), "provider_unavailable", True),
    ],
)
def test_apns_wake_failures_are_classified(error, code, transient):
    with pytest.raises(PushProviderError) as info:
        adapter(FakeTransport(error=error)).send_wake(device_token="t")
    assert (info.value.code, info.value.transient) == (code, transient)


def test_the_limiter_enforces_a_minimum_interval_and_an_hourly_cap_per_device():
    clock = Clock()
    limiter = pw.WakeLimiter(min_interval_seconds=60, max_per_hour=3, clock=clock)
    assert limiter.admit("a") and not limiter.admit("a")
    assert limiter.admit("b")
    clock.now += 61
    assert limiter.admit("a")
    clock.now += 61
    assert limiter.admit("a")
    clock.now += 61
    assert not limiter.admit("a")
    clock.now += 3600
    assert limiter.admit("a")


def test_the_limiter_forgets_idle_devices_when_it_is_full(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(pw, "MAX_TRACKED_DEVICES", 3)
    limiter = pw.WakeLimiter(min_interval_seconds=0, clock=clock)
    for name in "abc":
        assert limiter.admit(name)
    clock.now += 4000
    assert limiter.admit("d") and len(limiter._sent) == 1


def test_the_limiter_reads_its_limits_from_the_environment_and_ignores_nonsense():
    limiter = pw.WakeLimiter.from_env({pw.MIN_INTERVAL_ENV: "10", pw.MAX_PER_HOUR_ENV: "2"})
    assert (limiter._interval, limiter._cap) == (10.0, 2)
    bad = pw.WakeLimiter.from_env({pw.MIN_INTERVAL_ENV: "soon", pw.MAX_PER_HOUR_ENV: "-3"})
    assert (bad._interval, bad._cap) == (pw.DEFAULT_MIN_INTERVAL_SECONDS, pw.DEFAULT_MAX_PER_HOUR)
    zero = pw.WakeLimiter.from_env({pw.MAX_PER_HOUR_ENV: "0"})
    assert zero._cap == pw.DEFAULT_MAX_PER_HOUR


async def test_a_wake_is_sent_for_a_registered_ios_device():
    wake, transport = gateway()
    outcome = await wake.wake(OWNER, PHONE)
    assert outcome == pw.WakeOutcome("sent") and len(transport.calls) == 1
    assert transport.calls[0]["url"].endswith("/device-token-1")
    assert json.dumps(transport.calls[0]["json_body"]) == '{"aps": {"content-available": 1}}'


async def test_a_wake_without_a_registered_destination_sends_nothing():
    wake, transport = gateway(targets=Targets(None))
    assert (await wake.wake(OWNER, PHONE)).status == "no_destination" and not transport.calls


async def test_a_wake_for_a_provider_other_than_apns_is_not_sent():
    wake, transport = gateway(targets=Targets({"provider": "fcm", "push_token": "t"}))
    assert (await wake.wake(OWNER, PHONE)).status == "unsupported_provider" and not transport.calls
    wake, transport = gateway(targets=Targets({"provider": "other", "push_token": "t"}))
    assert (await wake.wake(OWNER, PHONE)).status == "unsupported_provider"


async def test_a_destination_store_failure_is_reported_without_sending():
    wake, transport = gateway(targets=Targets(error=RuntimeError("store down")))
    assert await wake.wake(OWNER, PHONE) == pw.WakeOutcome("failed", "destination_unavailable") and not transport.calls


async def test_a_provider_failure_is_reported_with_its_code():
    wake, _ = gateway(FakeTransport(error=BadRequestError("Upstream returned 410: {\"reason\": \"Unregistered\"}")))
    assert await wake.wake(OWNER, PHONE) == pw.WakeOutcome("failed", "token_unregistered")


async def test_wakes_are_rate_limited_per_device_and_per_owner():
    clock = Clock()
    wake, transport = gateway(limiter=pw.WakeLimiter(min_interval_seconds=300, clock=clock))
    assert (await wake.wake(OWNER, PHONE)).status == "sent"
    assert (await wake.wake(OWNER, PHONE)).status == "rate_limited"
    assert (await wake.wake("bob", PHONE)).status == "sent"
    assert (await wake.wake(OWNER, "sha256:" + "cd" * 32)).status == "sent"
    assert len(transport.calls) == 3


def test_the_destination_id_drops_the_hash_prefix_and_the_store_is_asked_for_that_device():
    asked = []
    store = SimpleNamespace(delivery_target=lambda owner, device: asked.append((owner, device)) or {"provider": "apns", "push_token": "t"})
    assert pw.destination_id(PHONE) == HEX and pw.destination_id("plain") == "plain"
    assert pw.DestinationTargets(store).target(OWNER, PHONE)["push_token"] == "t" and asked == [(OWNER, HEX)]


def test_a_gateway_exists_only_where_destinations_and_apns_credentials_do(monkeypatch):
    configured = {"apns": adapter(FakeTransport())}
    destinations = SimpleNamespace(delivery_target=lambda *a: None)
    assert pw.gateway_for(SimpleNamespace(), adapters=configured) is None
    assert pw.gateway_for(SimpleNamespace(push_destinations=destinations), adapters={"apns": ApnsPushAdapter(key_id="", team_id="", private_key="", topic="")}) is None
    assert pw.gateway_for(SimpleNamespace(push_destinations=destinations), adapters=configured) is not None
    monkeypatch.setenv(pw.ENABLE_ENV, "off")
    assert pw.gateway_for(SimpleNamespace(push_destinations=destinations), adapters=configured) is None


def test_the_default_adapters_come_from_the_push_environment(monkeypatch):
    for name in ("PUSH_APNS_KEY_ID", "PUSH_APNS_TEAM_ID", "PUSH_APNS_TOPIC"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PUSH_APNS_PRIVATE_KEY", raising=False)
    assert pw.gateway_for(SimpleNamespace(push_destinations=object())) is None


def peer(device_id, *, connected, pending_out):
    return SimpleNamespace(device_id=device_id, connected=connected, pending_out=pending_out)


class Mesh:
    def __init__(self, peers):
        self.peers = peers
        self.callbacks = []

    def subscribe(self, callback):
        self.callbacks.append(callback)

    async def sync_peers(self):
        return list(self.peers)


class Recorder:
    def __init__(self, status="sent"):
        self.calls = []
        self.status = status

    async def wake(self, owner, device):
        self.calls.append((owner, device))
        return pw.WakeOutcome(self.status)


async def test_a_disconnected_device_with_waiting_events_is_woken_once_per_new_batch():
    mesh, recorder = Mesh([peer(PHONE, connected=False, pending_out=2)]), Recorder()
    trigger = pw.WakeTrigger(mesh, recorder, lambda: OWNER)
    assert [o.status for o in await trigger.evaluate()] == ["sent"]
    assert await trigger.evaluate() == []
    mesh.peers = [peer(PHONE, connected=False, pending_out=5)]
    assert len(await trigger.evaluate()) == 1
    assert recorder.calls == [(OWNER, PHONE)] * 2


async def test_a_connected_device_or_one_with_nothing_waiting_is_never_woken():
    mesh, recorder = Mesh([peer(PHONE, connected=True, pending_out=9), peer("x", connected=False, pending_out=0)]), Recorder()
    trigger = pw.WakeTrigger(mesh, recorder, lambda: OWNER)
    assert await trigger.evaluate() == [] and recorder.calls == []


async def test_a_device_that_connects_and_later_disconnects_with_new_events_is_woken_again():
    mesh, recorder = Mesh([peer(PHONE, connected=False, pending_out=1)]), Recorder()
    trigger = pw.WakeTrigger(mesh, recorder, lambda: OWNER)
    await trigger.evaluate()
    mesh.peers = [peer(PHONE, connected=True, pending_out=0)]
    await trigger.evaluate()
    mesh.peers = [peer(PHONE, connected=False, pending_out=1)]
    await trigger.evaluate()
    assert len(recorder.calls) == 2


async def test_a_wake_that_was_rate_limited_is_tried_again_on_the_next_change():
    mesh, recorder = Mesh([peer(PHONE, connected=False, pending_out=1)]), Recorder("rate_limited")
    trigger = pw.WakeTrigger(mesh, recorder, lambda: OWNER)
    await trigger.evaluate()
    recorder.status = "sent"
    await trigger.evaluate()
    await trigger.evaluate()
    assert len(recorder.calls) == 2


async def test_a_wake_for_a_device_without_a_destination_is_not_retried_until_new_events_arrive():
    mesh, recorder = Mesh([peer(PHONE, connected=False, pending_out=1)]), Recorder("no_destination")
    trigger = pw.WakeTrigger(mesh, recorder, lambda: OWNER)
    await trigger.evaluate()
    await trigger.evaluate()
    assert len(recorder.calls) == 1


async def test_nothing_is_woken_before_the_node_knows_its_owner():
    mesh, recorder = Mesh([peer(PHONE, connected=False, pending_out=1)]), Recorder()
    assert await pw.WakeTrigger(mesh, recorder, lambda: None).evaluate() == [] and recorder.calls == []


async def test_a_running_trigger_wakes_on_a_kernel_change_and_survives_a_failing_evaluation():
    mesh, recorder = Mesh([]), Recorder()
    trigger = pw.WakeTrigger(mesh, recorder, lambda: OWNER)
    trigger.start()
    await settle(lambda: mesh.callbacks)
    mesh.peers = [peer(PHONE, connected=False, pending_out=1)]
    mesh.callbacks[0](object())
    await settle(lambda: recorder.calls)

    async def broken():
        raise RuntimeError("kernel busy")

    mesh.sync_peers = broken
    mesh.callbacks[0](object())
    await asyncio.sleep(0.05)
    mesh.sync_peers = Mesh([peer("other", connected=False, pending_out=1)]).sync_peers
    mesh.callbacks[0](object())
    await settle(lambda: len(recorder.calls) == 2)
    await trigger.stop()
    mesh.callbacks[0](object())


async def test_the_whole_path_from_a_kernel_change_to_a_content_free_apns_request(tmp_path):
    fake.Kernel.instances.clear()
    mesh = node_mesh.NodeMesh(data_dir=tmp_path, binding=fake, issuers=no_issuers, mailbox=StubMailbox())
    await mesh.start()
    kernel = fake.Kernel.instances[-1]
    kernel.join()
    transport = FakeTransport()
    wake, _ = gateway(transport)
    triggers = pw.WakeTriggers(wake)
    triggers.attach(mesh, lambda: OWNER)
    await asyncio.sleep(0.05)
    kernel.peers = [peer(PHONE, connected=False, pending_out=3)]
    kernel.notify()
    await settle(lambda: transport.calls)
    assert transport.calls[0]["json_body"] == {"aps": {"content-available": 1}}
    kernel.peers = [peer(PHONE, connected=False, pending_out=4)]
    kernel.notify()
    await asyncio.sleep(0.2)
    assert len(transport.calls) == 2
    await triggers.stop()
    await mesh.stop()


async def test_each_institution_owner_gets_a_trigger_that_wakes_for_that_owner_only(tmp_path, monkeypatch):
    fake.Kernel.instances.clear()
    transport = FakeTransport()
    targets = Targets(APNS)
    wake = pw.WakeGateway(targets=targets, adapters={"apns": adapter(transport)}, limiter=pw.WakeLimiter(min_interval_seconds=0))
    monkeypatch.setenv(ni.PROFILE_ENV, "institution")
    runtime = institution_runtime.InstitutionRuntime(
        config=ip.InstitutionConfig(key="k", issuers=("i",)),
        base_dir=tmp_path,
        history=History(),
        runner=answering(),
        calls=None,
        binding=fake,
        issuers=no_issuers,
        settings=node_mesh.MeshSettings(mailbox=node_mesh.MailboxSettings(enabled=False)),
        phi_active=lambda _owner: False,
        wake=pw.WakeTriggers(wake),
    )
    await runtime.serve_owner(Owner.verified("https://idp.example/realms/a", "alice"))
    await runtime.serve_owner(Owner.verified("https://idp.example/realms/a", "bob"))
    await asyncio.sleep(0.05)
    fake.Kernel.instances[1].peers = [peer(PHONE, connected=False, pending_out=1)]
    fake.Kernel.instances[1].notify()
    await settle(lambda: targets.asked)
    assert targets.asked == [(ni.canonical_owner_id("https://idp.example/realms/a", "bob"), PHONE)]
    await runtime.stop()
    nmc.install(None)


async def test_the_personal_node_attaches_a_trigger_and_stops_it_with_the_bridge(tmp_path, monkeypatch):
    fake.Kernel.instances.clear()
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.example/realms/a")
    monkeypatch.setenv("ASTRAL_NODE_MAILBOX", "off")
    ni.reset_cache()
    ni.bind_owner("oidc.abc.owner")
    monkeypatch.setattr(node_mesh, "load_binding", lambda: fake)
    transport = FakeTransport()
    wake = pw.WakeGateway(targets=Targets(APNS), adapters={"apns": adapter(transport)}, limiter=pw.WakeLimiter(min_interval_seconds=0))
    monkeypatch.setattr(pw, "gateway_for", lambda orch: wake)
    await nmc.start_for(SimpleNamespace(history=History()))
    kernel = fake.Kernel.instances[-1]
    kernel.join()
    await asyncio.sleep(0.05)
    kernel.peers = [peer(PHONE, connected=False, pending_out=1)]
    kernel.notify()
    await settle(lambda: transport.calls)
    await nmc.stop_all()
    assert nmc._wake is None
    ni.reset_cache()
