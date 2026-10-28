"""Bounded FCM/APNs provider adapters: runtime-secret configuration, egress through
shared.external_http, sender-credential JWT caching, and transient versus permanent
failure classification for expired credentials, unregistered tokens, rate limits,
and provider outages.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives import serialization
from shared.external_http import (
    AuthFailedError,
    BadRequestError,
    ExternalHttpError,
    RateLimitedError,
    ServiceUnreachableError,
)

from orchestrator import push_providers as pp
from orchestrator.push_providers import (
    ApnsPushAdapter,
    FcmPushAdapter,
    PushProviderError,
    adapters_from_env,
    adapters_from_settings,
    settings_from_env,
)


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class FakeTransport:
    def __init__(self, responses=None, error=None):
        self.calls = []
        self.responses = list(responses or [])
        self.error = error

    def __call__(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.error is not None:
            raise self.error
        if not self.responses:
            return FakeResponse()
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _pem_rsa() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _pem_ec() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def test_fcm_adapter_configured_property():
    unconfigured = FcmPushAdapter(project_id="", client_email="", private_key="")
    assert unconfigured.configured is False
    configured = FcmPushAdapter(
        project_id="proj", client_email="sa@proj.iam", private_key="key"
    )
    assert configured.configured is True


def test_fcm_send_success_and_token_cache():
    transport = FakeTransport(
        responses=[FakeResponse(text='{"access_token": "tok-1"}'), FakeResponse()]
    )
    adapter = FcmPushAdapter(
        project_id="proj",
        client_email="sa@proj.iam",
        private_key=_pem_rsa(),
        transport=transport,
        clock=lambda: 1000.0,
    )
    adapter.send(
        device_token="dev-token",
        title="Task finished",
        body="All good",
        notification_id="notif-1",
    )
    adapter.send(
        device_token="dev-token",
        title="Task finished",
        body="All good",
        notification_id="notif-1",
    )
    token_calls = [call for call in transport.calls if "oauth2" in call["url"]]
    send_calls = [call for call in transport.calls if "fcm.googleapis" in call["url"]]
    assert len(token_calls) == 1
    assert len(send_calls) == 2
    send = send_calls[0]
    assert send["api_key"] == "tok-1"
    message = send["json_body"]["message"]
    assert message["token"] == "dev-token"
    assert "notification" not in message
    assert message["android"] == {"ttl": "300s"}
    assert message["data"] == {"notification_id": "notif-1"}


def test_fcm_token_exchange_rejection_is_transient_credential_error():
    adapter = FcmPushAdapter(
        project_id="proj",
        client_email="sa@proj.iam",
        private_key=_pem_rsa(),
        transport=FakeTransport(error=AuthFailedError("Authentication failed (401)")),
        clock=lambda: 1000.0,
    )
    with pytest.raises(PushProviderError) as excinfo:
        adapter.send(
            device_token="dev", title="t", body="b", notification_id="n1"
        )
    assert excinfo.value.transient is True
    assert excinfo.value.code == "provider_credentials_rejected"


def test_fcm_token_exchange_outage_is_transient():
    adapter = FcmPushAdapter(
        project_id="proj",
        client_email="sa@proj.iam",
        private_key=_pem_rsa(),
        transport=FakeTransport(error=ServiceUnreachableError("no route")),
        clock=lambda: 1000.0,
    )
    with pytest.raises(PushProviderError) as excinfo:
        adapter.send(device_token="dev", title="t", body="b", notification_id="n1")
    assert excinfo.value.transient is True
    assert excinfo.value.code == "provider_unavailable"


def test_fcm_token_exchange_without_access_token_is_transient():
    adapter = FcmPushAdapter(
        project_id="proj",
        client_email="sa@proj.iam",
        private_key=_pem_rsa(),
        transport=FakeTransport(responses=[FakeResponse(text='{"error": "nope"}')]),
        clock=lambda: 1000.0,
    )
    with pytest.raises(PushProviderError) as excinfo:
        adapter.send(device_token="dev", title="t", body="b", notification_id="n1")
    assert excinfo.value.code == "provider_unavailable"


@pytest.mark.parametrize(
    ("status", "body", "expected_code", "expected_transient"),
    [
        (404, '{"error": {"status": "UNREGISTERED"}}', "token_unregistered", False),
        (400, '{"error": {"status": "SENDER_ID_MISMATCH"}}', "token_sender_mismatch", False),
        (400, '{"error": {"status": "INVALID_ARGUMENT"}}', "request_rejected", False),
        (401, '{"error": {"status": "THIRD_PARTY_AUTH_ERROR"}}', "request_rejected", False),
        (400, '{"error": {"status": "INTERNAL"}}', "provider_unavailable", True),
    ],
)
def test_fcm_send_failure_classification(status, body, expected_code, expected_transient):
    transport = FakeTransport(
        responses=[
            FakeResponse(text='{"access_token": "tok-1"}'),
            BadRequestError(f"Upstream returned {status}: {body}")
            if 400 <= status < 500
            else FakeResponse(),
        ]
    )
    adapter = FcmPushAdapter(
        project_id="proj",
        client_email="sa@proj.iam",
        private_key=_pem_rsa(),
        transport=transport,
        clock=lambda: 1000.0,
    )
    with pytest.raises(PushProviderError) as excinfo:
        adapter.send(device_token="dev", title="t", body="b", notification_id="n1")
    assert excinfo.value.code == expected_code
    assert excinfo.value.transient is expected_transient


def test_fcm_send_rate_limit_and_outage_are_transient():
    for error in [
        RateLimitedError("Rate-limited by upstream (429)"),
        RateLimitedError("Upstream server error (500)"),
        ServiceUnreachableError("timeout"),
    ]:
        adapter = FcmPushAdapter(
            project_id="proj",
            client_email="sa@proj.iam",
            private_key=_pem_rsa(),
            transport=FakeTransport(
                responses=[FakeResponse(text='{"access_token": "tok-1"}'), error]
            ),
            clock=lambda: 1000.0,
        )
        with pytest.raises(PushProviderError) as excinfo:
            adapter.send(device_token="dev", title="t", body="b", notification_id="n1")
        assert excinfo.value.transient is True
        assert excinfo.value.code == "provider_unavailable"


def test_fcm_send_credential_rejection_invalidates_cached_token():
    transport = FakeTransport(
        responses=[
            FakeResponse(text='{"access_token": "tok-1"}'),
            AuthFailedError("Authentication failed (401)"),
            FakeResponse(text='{"access_token": "tok-2"}'),
            FakeResponse(),
        ]
    )
    adapter = FcmPushAdapter(
        project_id="proj",
        client_email="sa@proj.iam",
        private_key=_pem_rsa(),
        transport=transport,
        clock=lambda: 1000.0,
    )
    with pytest.raises(PushProviderError):
        adapter.send(device_token="dev", title="t", body="b", notification_id="n1")
    adapter.send(device_token="dev", title="t", body="b", notification_id="n1")
    token_calls = [call for call in transport.calls if "oauth2" in call["url"]]
    assert len(token_calls) == 2


def test_fcm_unconfigured_send_is_transient_unconfigured():
    adapter = FcmPushAdapter(project_id="", client_email="", private_key="")
    with pytest.raises(PushProviderError) as excinfo:
        adapter.send(device_token="dev", title="t", body="b", notification_id="n1")
    assert excinfo.value.code == "provider_unconfigured"
    assert excinfo.value.transient is True


def test_apns_send_success_with_required_headers():
    transport = FakeTransport(responses=[FakeResponse()])
    adapter = ApnsPushAdapter(
        key_id="KEYID",
        team_id="TEAM",
        private_key=_pem_ec(),
        topic="com.astral.app",
        transport=transport,
        clock=lambda: 1000.0,
    )
    adapter.send(
        device_token="ios-device-token",
        title="Hello",
        body="World",
        notification_id="notif-9",
    )
    call = transport.calls[0]
    assert call["url"] == "https://api.push.apple.com/3/device/ios-device-token"
    assert call["extra_headers"]["apns-topic"] == "com.astral.app"
    assert call["extra_headers"]["apns-push-type"] == "alert"
    assert call["json_body"]["aps"] == {"alert": {"title": "AstralDeep",
                                                       "body": "You have a new notification."}}
    assert call["extra_headers"]["apns-expiration"] == "1300"
    assert len(call["extra_headers"]["apns-collapse-id"].encode()) <= 64
    assert call["json_body"]["notification_id"] == "notif-9"


def test_apns_provider_token_cached_until_lifetime():
    transport = FakeTransport(responses=[FakeResponse(), FakeResponse(), FakeResponse()])
    now = [1000.0]
    adapter = ApnsPushAdapter(
        key_id="KEYID",
        team_id="TEAM",
        private_key=_pem_ec(),
        topic="com.astral.app",
        transport=transport,
        clock=lambda: now[0],
    )
    adapter.send(device_token="d1", title="t", body="b", notification_id="n1")
    first_token = adapter._cached_token
    adapter.send(device_token="d2", title="t", body="b", notification_id="n2")
    assert adapter._cached_token is first_token
    now[0] = 1000.0 + pp.APNS_TOKEN_LIFETIME_SECONDS + 1
    adapter.send(device_token="d3", title="t", body="b", notification_id="n3")
    assert adapter._cached_token is not first_token


@pytest.mark.parametrize(
    ("status", "body", "expected_code", "expected_transient"),
    [
        (410, '{"reason": "Unregistered"}', "token_unregistered", False),
        (400, '{"reason": "BadDeviceToken"}', "token_unregistered", False),
        (403, '{"reason": "InvalidProviderToken"}', "provider_credentials_rejected", True),
        (400, '{"reason": "BadTopic"}', "request_rejected", False),
        (400, '{"reason": "PayloadTooLarge"}', "request_rejected", False),
        (400, '{"reason": "MalformedPayload"}', "request_rejected", False),
    ],
)
def test_apns_failure_classification(status, body, expected_code, expected_transient):
    adapter = ApnsPushAdapter(
        key_id="KEYID",
        team_id="TEAM",
        private_key=_pem_ec(),
        topic="com.astral.app",
        transport=FakeTransport(
            error=BadRequestError(f"Upstream returned {status}: {body}")
        ),
        clock=lambda: 1000.0,
    )
    with pytest.raises(PushProviderError) as excinfo:
        adapter.send(device_token="dev", title="t", body="b", notification_id="n1")
    assert excinfo.value.code == expected_code
    assert excinfo.value.transient is expected_transient


def test_apns_credential_and_outage_errors_are_transient():
    for error in [
        AuthFailedError("Authentication failed (403)"),
        RateLimitedError("Rate-limited by upstream (429)"),
        ServiceUnreachableError("connect timeout"),
    ]:
        adapter = ApnsPushAdapter(
            key_id="KEYID",
            team_id="TEAM",
            private_key=_pem_ec(),
            topic="com.astral.app",
            transport=FakeTransport(error=error),
            clock=lambda: 1000.0,
        )
        with pytest.raises(PushProviderError) as excinfo:
            adapter.send(device_token="dev", title="t", body="b", notification_id="n1")
        assert excinfo.value.transient is True


def test_apns_http2_transport_missing_is_transient_unavailable(monkeypatch):
    def broken_transport(*args, **kwargs):
        raise ExternalHttpError("http2 transport is unavailable")

    adapter = ApnsPushAdapter(
        key_id="KEYID",
        team_id="TEAM",
        private_key=_pem_ec(),
        topic="com.astral.app",
        transport=broken_transport,
        clock=lambda: 1000.0,
    )
    with pytest.raises(PushProviderError) as excinfo:
        adapter.send(device_token="dev", title="t", body="b", notification_id="n1")
    assert excinfo.value.transient is True
    assert excinfo.value.code == "provider_unavailable"


def test_apns_unconfigured_send_is_transient_unconfigured():
    adapter = ApnsPushAdapter(key_id="", team_id="", private_key="", topic="")
    with pytest.raises(PushProviderError) as excinfo:
        adapter.send(device_token="dev", title="t", body="b", notification_id="n1")
    assert excinfo.value.code == "provider_unconfigured"


def test_settings_and_adapters_from_env(monkeypatch):
    monkeypatch.delenv("PUSH_FCM_PROJECT_ID", raising=False)
    monkeypatch.delenv("PUSH_APNS_KEY_ID", raising=False)
    empty = adapters_from_env(environ={})
    assert empty["fcm"].configured is False
    assert empty["apns"].configured is False
    settings = settings_from_env(
        environ={
            "PUSH_FCM_PROJECT_ID": "proj",
            "PUSH_FCM_CLIENT_EMAIL": "sa@proj.iam",
            "PUSH_FCM_PRIVATE_KEY": "key",
            "PUSH_APNS_KEY_ID": "KID",
            "PUSH_APNS_TEAM_ID": "TEAM",
            "PUSH_APNS_PRIVATE_KEY": "p8",
            "PUSH_APNS_TOPIC": "com.astral.app",
            "PUSH_APNS_HOST": "https://api.push.apple.com",
        }
    )
    adapters = adapters_from_settings(settings)
    assert adapters["fcm"].configured is True
    assert adapters["apns"].configured is True


def test_provider_error_detail_is_sanitized():
    error = PushProviderError(
        transient=True, code="provider_unavailable", detail="boom\r\nsecret\nline"
    )
    assert "\n" not in error.detail and "\r" not in error.detail
    assert len(error.detail) <= 200


class _FakeHttpxResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self._text = text

    @property
    def content(self):
        return self._text.encode()

    @property
    def text(self):
        return self._text

    def iter_bytes(self, *, chunk_size):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset:offset + chunk_size]


class _FakeHttpxClient:
    def __init__(self, response=None, error=None, **kwargs):
        self.kwargs = kwargs
        self.requests = []
        self._response = response
        self._error = error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, method, url, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        if self._error is not None:
            raise self._error
        return self._response

    @contextmanager
    def stream(self, method, url, **kwargs):
        yield self.request(method, url, **kwargs)


@pytest.fixture()
def no_egress_check(monkeypatch):
    import shared.external_http as eh

    monkeypatch.setattr(eh, "validate_egress_url", lambda url, **kwargs: None)


def test_request_http2_success_and_bearer(monkeypatch, no_egress_check):
    import shared.external_http as eh

    client_kwargs = {}
    fake = _FakeHttpxClient(response=_FakeHttpxResponse(text="ok"))

    def make_client(**kwargs):
        client_kwargs.update(kwargs)
        return fake

    monkeypatch.setattr(eh.httpx, "Client", make_client)
    response = eh.request_http2(
        "POST",
        "https://api.push.apple.com/3/device/token",
        api_key="bearer-tok",
        json_body={"aps": {}},
        extra_headers={"apns-topic": "com.astral.app"},
    )
    assert response.status_code == 200
    assert response.text == "ok"
    call = fake.requests[0]
    assert call["method"] == "POST"
    assert call["headers"]["Authorization"] == "Bearer bearer-tok"
    assert call["headers"]["apns-topic"] == "com.astral.app"
    assert call["json"] == {"aps": {}}
    assert client_kwargs["http2"] is True


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, eh_auth_failed := "AuthFailedError"),
        (429, "RateLimitedError"),
        (503, "RateLimitedError"),
        (410, "BadRequestError"),
    ],
)
def test_request_http2_status_classification(monkeypatch, no_egress_check, status, expected):
    import shared.external_http as eh

    fake = _FakeHttpxClient(response=_FakeHttpxResponse(status_code=status, text="body"))
    monkeypatch.setattr(eh.httpx, "Client", lambda **kwargs: fake)
    with pytest.raises(
        {
            "AuthFailedError": eh.AuthFailedError,
            "RateLimitedError": eh.RateLimitedError,
            "BadRequestError": eh.BadRequestError,
        }[expected]
    ):
        eh.request_http2("POST", "https://api.push.apple.com/3/device/token")


def test_request_http2_transport_error_maps_to_unreachable(monkeypatch, no_egress_check):
    import shared.external_http as eh

    fake = _FakeHttpxClient(error=eh.httpx.ConnectError("refused"))
    monkeypatch.setattr(eh.httpx, "Client", lambda **kwargs: fake)
    with pytest.raises(eh.ServiceUnreachableError):
        eh.request_http2("POST", "https://api.push.apple.com/3/device/token")


def test_request_http2_response_too_large(monkeypatch, no_egress_check):
    import shared.external_http as eh

    fake = _FakeHttpxClient(response=_FakeHttpxResponse(text="x" * 64))
    monkeypatch.setattr(eh.httpx, "Client", lambda **kwargs: fake)
    with pytest.raises(eh.ResponseTooLargeError):
        eh.request_http2(
            "POST",
            "https://api.push.apple.com/3/device/token",
            max_response_bytes=8,
        )


def test_request_http2_missing_h2_dependency_fails_closed(monkeypatch, no_egress_check):
    import shared.external_http as eh

    def broken(**kwargs):
        raise ImportError("h2 package is not installed")

    monkeypatch.setattr(eh.httpx, "Client", broken)
    with pytest.raises(eh.ExternalHttpError):
        eh.request_http2("POST", "https://api.push.apple.com/3/device/token")


def test_request_http2_validates_egress(monkeypatch):
    import shared.external_http as eh

    checked = []

    def spy(url, **kwargs):
        checked.append(url)
        raise eh.EgressBlockedError("blocked by policy")

    monkeypatch.setattr(eh, "validate_egress_url", spy)
    with pytest.raises(eh.EgressBlockedError):
        eh.request_http2("POST", "http://127.0.0.1:9000/exfiltrate")
    assert checked == ["http://127.0.0.1:9000/exfiltrate"]


def test_request_http2_stops_reading_at_the_response_size_bound(monkeypatch, no_egress_check):
    import httpx
    import shared.external_http as eh

    class LargeStream(httpx.SyncByteStream):
        def __init__(self):
            self.reads = 0
            self.closed = False

        def __iter__(self):
            for _ in range(100):
                self.reads += 1
                yield b"x" * 65536

        def close(self):
            self.closed = True

    stream = LargeStream()
    client_type = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))

    def make_client(**kwargs):
        kwargs["http2"] = False
        return client_type(transport=transport, **kwargs)

    monkeypatch.setattr(eh.httpx, "Client", make_client)
    with pytest.raises(eh.ResponseTooLargeError):
        eh.request_http2(
            "POST", "https://api.push.apple.com/3/device/token", max_response_bytes=65536
        )
    assert stream.closed
    assert stream.reads <= 2


@pytest.mark.parametrize("provider", ["fcm", "apns"])
def test_provider_expiry_is_bounded_by_remaining_server_validity(provider):
    transport = FakeTransport(responses=[FakeResponse(text='{"access_token":"oauth"}'), FakeResponse()]
                              if provider == "fcm" else [FakeResponse()])
    if provider == "fcm":
        adapter = FcmPushAdapter(project_id="proj", client_email="sa@proj.iam", private_key=_pem_rsa(),
                                 transport=transport, clock=lambda: 1000.0)
    else:
        adapter = ApnsPushAdapter(key_id="KEYID", team_id="TEAM", private_key=_pem_ec(), topic="com.astral.app",
                                  transport=transport, clock=lambda: 1000.0)
    adapter.send(device_token="device", title="Patient private", body="MRN secret", notification_id="opaque-event",
                 expires_at=1020)
    call = transport.calls[-1]
    serialized = json.dumps(call["json_body"])
    assert "Patient" not in serialized and "MRN" not in serialized
    if provider == "fcm":
        assert call["json_body"]["message"]["android"]["ttl"] == "20s"
        assert call["json_body"]["message"]["data"] == {"notification_id": "opaque-event"}
    else:
        assert call["extra_headers"]["apns-expiration"] == "1020"
    calls = len(transport.calls)
    with pytest.raises(PushProviderError) as error:
        adapter.send(device_token="device", title="private", body="private", notification_id="expired", expires_at=999)
    assert error.value.code == "notification_expired"
    assert len(transport.calls) == calls
