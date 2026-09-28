"""Clock controls for tests whose product code compares expiry against database or host
time: read or wait on the PostgreSQL clock, or advance the samples that AstralPlane session
guards and request-principal checks take once a test's condition holds, without sleeping.
"""

import asyncio
from dataclasses import replace


def database_now(runtime):
    with runtime.transaction() as transaction:
        return transaction.fetch_one("SELECT clock_timestamp() AS now")["now"]


async def wait_for_database_time(runtime, target):
    while (remaining := (target - await asyncio.to_thread(database_now, runtime)).total_seconds()) > 0:
        await asyncio.sleep(remaining)


def advance_session_clock(monkeypatch, repository, *, to, when):
    sample = repository._assert_credential

    def observed(transaction, credential):
        state = sample(transaction, credential)
        return replace(state, observed_at=max(state.observed_at, to)) if when() else state

    monkeypatch.setattr(repository, "_assert_credential", observed)


def expire_principal(monkeypatch, *, at, when):
    from orchestrator.work_submit_authority import AuthenticatedWorkRequest

    check = AuthenticatedWorkRequest.assert_current

    def crossed(self, runtime, *, now=None):
        if when() and self.principal_expires_at == at:
            now = at if now is None else max(now, at)
        return check(self, runtime, now=now)

    monkeypatch.setattr(AuthenticatedWorkRequest, "assert_current", crossed)


class NoteClock:
    def __init__(self, expiry):
        self.offset_ms = 0
        self.expiry = expiry

    def cross_expiry(self):
        self.offset_ms = 60_000


def explicit_note_clock(monkeypatch, runtime):
    from astralplane.repositories import guidance
    from personalization import explicit_note_service

    database, host = guidance._clock, explicit_note_service._now
    with runtime.transaction() as transaction:
        clock = NoteClock(database(transaction) + 60_000)
    monkeypatch.setattr(guidance, "_clock", lambda query: database(query) + clock.offset_ms)
    monkeypatch.setattr(explicit_note_service, "_now", lambda: host() + clock.offset_ms)
    return clock
