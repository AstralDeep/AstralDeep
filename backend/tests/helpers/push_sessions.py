"""Real Plane-backed consenting sessions for push destination and delivery tests.
The bound store supplies genuine consent observations while legacy calls can explicitly
select no session to exercise fail-closed compatibility behavior.
"""

from datetime import timedelta
import uuid

from astralplane.repositories.history import SessionConsentObservation
import pytest
from orchestrator.push_destinations import PushDestinationStore
from orchestrator.session_consent import ConsentSession
from orchestrator.session_store import WebSessionStore


@pytest.fixture(autouse=True)
def push_iam_policy(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://issuer.example/realms/test")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-web")
    monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", "native-client")


def create_consent(sessions, owner, sid=None):
    sid = sid or uuid.uuid4().hex
    sessions.create(sid, user_id=owner, access_token="synthetic-access", refresh_token="synthetic-refresh",
                    hard_max_seconds=3600, issuing_issuer="https://issuer.example/realms/test",
                    issuing_client_id="native-client")
    return sid, current_consent(sessions, owner, sid)


consent_session = create_consent


def current_consent(sessions, owner, sid):
    state = sessions.capture_execution_reference(owner_id=owner, session_id=sid).state
    return ConsentSession(SessionConsentObservation(state.credential, state.observed_at,
                                                    state.observed_at + timedelta(seconds=15)))


class SessionBoundPushStore(PushDestinationStore):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sessions = WebSessionStore(plane_runtime=self._credentials.plane_runtime)
        self.session_ids = {}

    def enroll(self, owner_id, *, consent_session="test-default", **kwargs):
        if consent_session == "test-default":
            if owner_id not in self.session_ids:
                sid, consent_session = create_consent(self.sessions, owner_id)
                self.session_ids[owner_id] = sid
            else:
                consent_session = current_consent(self.sessions, owner_id, self.session_ids[owner_id])
        return super().enroll(owner_id, consent_session=consent_session, **kwargs)
