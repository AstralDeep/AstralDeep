"""Owner-authorized selection uses typed Plane preferences and captures immutable new-work identities.
Current human requests fence selection and audit; runners recheck captured configuration and packaged facts.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from astralplane.repositories import RepositoryConflictError
from audit.pii import private_binding_key
from audit.schemas import AuditEventCreate, AuditEventDTO
from llm_config import research_profile
from orchestrator.harness.production_profiles import (
    HarnessSelectionError, deployment_for, deployment_for_capture, public_profiles,
)
from shared.feature_flags import flags


@dataclass(frozen=True, slots=True, repr=False)
class HarnessChoice:
    selection: object
    deployment: object = None


@dataclass(frozen=True, slots=True, repr=False)
class HarnessAdmission:
    selection: object
    capture: object = None
    config: object = None
    key: object = None
    origin: object = None


def _owner(value):
    if (type(value) is not str or not 1 <= len(value) <= 512 or value != value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise HarnessSelectionError("harness_owner_invalid", 403)


def _revision(value):
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise HarnessSelectionError("harness_selection_invalid", 422)


def _selected_config(fact, capture, store, key):
    selected = research_profile.select_config(capture, store=store, binding_key=key,
                                               profile=fact.profile)
    row = capture._record
    if (row.provider != fact.provider or selected.profile.model != fact.model
            or selected.key_id != fact.config_binding_key_id
            or selected.revision != fact.config_revision):
        raise HarnessSelectionError("harness_config_changed")
    return selected


class HarnessSelectionService:
    def __init__(self, orchestrator=None, *, config_store=None, plane_runtime=None):
        self.orch = orchestrator
        plane = getattr(getattr(orchestrator, "runtime_composition", None), "plane", None)
        self.config_store = (getattr(orchestrator, "_llm_store", None)
                             if orchestrator is not None else config_store)
        context = getattr(self.config_store, "_repository", None)
        self.runtime = (getattr(plane, "runtime", None) or plane_runtime
                        or getattr(context, "plane_runtime", None))
        self.repositories = getattr(self.runtime, "repositories", None)
        self.repository = getattr(getattr(self.repositories, "preferences", None), "harness", None)
        self.audit = getattr(orchestrator, "audit_repo", None)
        self._current(initial=True)

    @staticmethod
    def enabled():
        return flags.is_enabled("harness_profile_selection") is True

    def _current(self, *, initial=False):
        if (self.runtime is None
                or getattr(self.runtime, "repositories", None) is not self.repositories
                or self.repositories is None):
            raise HarnessSelectionError()
        if (getattr(getattr(self.repositories, "preferences", None), "harness", None)
                is not self.repository):
            raise HarnessSelectionError()
        if self.orch is not None:
            plane = getattr(getattr(self.orch, "runtime_composition", None), "plane", None)
            if ((not initial and getattr(self.orch, "harness_selection", None) is not self)
                    or getattr(plane, "runtime", None) is not self.runtime
                    or getattr(plane, "repositories", None) is not self.repositories
                    or getattr(self.orch, "audit_repo", None) is not self.audit
                    or getattr(self.orch, "_llm_store", None) is not self.config_store):
                raise HarnessSelectionError()
        if self.config_store is not None:
            context = self.config_store._repository
            if (context.plane_runtime is not self.runtime
                    or context.repository is not self.repositories.encrypted_llm_config):
                raise HarnessSelectionError()

    def _caller(self, caller, *, write=False):
        from orchestrator.human_request_authority import CurrentHumanCaller
        self._current()
        if (type(caller) is not CurrentHumanCaller or self.orch is None
                or getattr(self.orch, "harness_selection", None) is not self
                or caller.plane_runtime is not self.runtime):
            raise HarnessSelectionError("harness_owner_invalid", 403)
        _owner(caller.owner_id)
        if write:
            caller.require_write()
        if self.repository is None:
            raise HarnessSelectionError("harness_component_unavailable")

    def _public(self, selection):
        return {"selection": {"harness_id": None if selection.identity is None
                              else selection.identity.profile, "revision": selection.revision},
                "enabled": self.enabled(), "profiles": public_profiles()}

    async def status(self, caller):
        self._caller(caller)
        selection = await caller.transaction(lambda tx, _: self.repository.get(
            tx, owner_id=caller.owner_id), expected_orchestrator=self.orch)
        self._caller(caller)
        result = self._public(selection)
        await caller.verify_delivery()
        self._caller(caller)
        return result

    async def select(self, caller, *, harness_id, expected_revision):
        self._caller(caller, write=True)
        _revision(expected_revision)
        if harness_id is not None and not self.enabled():
            raise HarnessSelectionError("harness_selection_disabled", 409)
        fact = None if harness_id is None else deployment_for(harness_id)

        def apply(tx, _):
            self._caller(caller, write=True)
            if fact is not None:
                if not self.enabled() or deployment_for(harness_id) != fact:
                    raise HarnessSelectionError("harness_selection_disabled", 409)
            try:
                selected = self.repository.compare_and_set(tx, owner_id=caller.owner_id,
                    expected_revision=expected_revision,
                    identity=None if fact is None else fact.identity,
                    qualification_digest=None if fact is None else fact.qualification_digest)
            except RepositoryConflictError:
                raise HarnessSelectionError("harness_selection_conflict", 409) from None
            now = datetime.now(UTC)
            event = AuditEventCreate(event_id=str(uuid4()), actor_user_id=caller.owner_id,
                auth_principal=caller.owner_id, event_class="settings", action_type="harness.selection",
                description="Harness selection changed", correlation_id=str(uuid4()),
                inputs_meta={"harness_id": harness_id, "expected_revision": expected_revision},
                outputs_meta={"revision": selected.revision}, outcome="success",
                started_at=now, completed_at=now)
            if self.audit is None:
                raise HarnessSelectionError()
            receipt = self.audit.insert_in_transaction(event, transaction=tx, plane_runtime=self.runtime)
            if (type(receipt) is not AuditEventDTO or receipt.event_id != event.event_id
                    or receipt.correlation_id != event.correlation_id
                    or receipt.event_class != event.event_class
                    or receipt.action_type != event.action_type or receipt.outcome != "success"
                    or receipt.inputs_meta != event.inputs_meta or receipt.outputs_meta != event.outputs_meta):
                raise HarnessSelectionError()
            self._caller(caller, write=True)
            return selected

        selection = await caller.transaction(apply, expected_orchestrator=self.orch)
        await caller.verify_delivery()
        self._caller(caller)
        return self._public(selection)

    def choice_sync(self, owner_id, transaction=None):
        self._current()
        _owner(owner_id)
        if self.repository is None:
            if self.enabled():
                raise HarnessSelectionError("harness_component_unavailable")
            return None
        try:
            if transaction is None:
                with self.runtime.transaction() as tx:
                    selection = self.repository.get(tx, owner_id=owner_id)
            else:
                selection = self.repository.get(transaction, owner_id=owner_id)
            self._current()
            if selection.identity is None:
                return HarnessChoice(selection)
            if not self.enabled():
                raise HarnessSelectionError("harness_selection_disabled", 409)
            fact = deployment_for(selection.identity.profile)
            if (fact.identity != selection.identity
                    or fact.qualification_digest != selection.qualification_digest):
                raise HarnessSelectionError("harness_capture_changed")
            return HarnessChoice(selection, fact)
        except HarnessSelectionError:
            raise
        except Exception:
            raise HarnessSelectionError() from None

    def prepare_choice_sync(self, choice, transaction=None):
        self._current()
        if choice is None:
            if self.repository is not None or self.enabled():
                raise HarnessSelectionError("harness_capture_invalid")
            return None
        if type(choice) is not HarnessChoice:
            raise HarnessSelectionError("harness_capture_invalid")
        if choice.deployment is None:
            return HarnessAdmission(choice.selection)
        try:
            if (not self.enabled() or deployment_for(choice.selection.identity.profile)
                    != choice.deployment):
                raise HarnessSelectionError("harness_selection_disabled", 409)
            if self.config_store is None:
                raise HarnessSelectionError("harness_config_unavailable")
            key = private_binding_key()
            fact = choice.deployment
            captured_config = self.config_store.capture_user_sync(
                choice.selection.owner_id, transaction=transaction)
            config = _selected_config(fact, captured_config, self.config_store, key)
            from astralplane.repositories.harness_capture import HarnessCapture
            captured = HarnessCapture(choice.selection.owner_id, choice.selection.revision,
                fact.identity, fact.qualification_digest, fact.profile.name, key.key_id, config.revision)
            self._current()
            return HarnessAdmission(choice.selection, captured, config, key)
        except HarnessSelectionError:
            raise
        except Exception:
            raise HarnessSelectionError() from None

    def prepare_sync(self, owner_id, transaction=None):
        return self.prepare_choice_sync(self.choice_sync(owner_id, transaction=transaction),
                                        transaction=transaction)

    async def choice(self, owner_id):
        return await asyncio.to_thread(self.choice_sync, owner_id)

    async def prepare_choice(self, choice):
        return await asyncio.to_thread(self.prepare_choice_sync, choice)

    async def prepare(self, owner_id):
        return await asyncio.to_thread(self.prepare_sync, owner_id)

    def assert_current(self, tx, admission):
        self._current()
        if admission is None:
            if self.repository is not None or self.enabled():
                raise HarnessSelectionError("harness_capture_invalid")
            return
        if type(admission) is not HarnessAdmission or self.repository is None:
            raise HarnessSelectionError("harness_capture_invalid")
        self.assert_origin(admission)
        try:
            self.repository.assert_current(tx, owner_id=admission.selection.owner_id,
                                           expected=admission.selection)
            if admission.capture is not None:
                if not self.enabled():
                    raise HarnessSelectionError("harness_selection_disabled", 409)
                deployment_for_capture(admission.capture)
                if (private_binding_key(admission.key.key_id) != admission.key
                        or not admission.config.matches(self.repositories.encrypted_llm_config.get_user_for_update(
                            tx, owner_id=admission.selection.owner_id))):
                    raise HarnessSelectionError("harness_config_changed")
            self._current()
        except HarnessSelectionError:
            raise
        except RepositoryConflictError:
            raise HarnessSelectionError("harness_selection_conflict", 409) from None
        except Exception:
            raise HarnessSelectionError() from None

    def assert_captured_current(self, tx, captured):
        self._current()
        if captured is None:
            return
        fact = deployment_for_capture(captured)
        if self.config_store is None:
            raise HarnessSelectionError("harness_config_unavailable")
        key = private_binding_key(captured.config_binding_key_id)
        config = _selected_config(fact, self.config_store.capture_user_sync(
            captured.owner_id, transaction=tx), self.config_store, key)
        if (config.revision != captured.config_revision or not config.matches(
                self.repositories.encrypted_llm_config.get_user_for_update(tx, owner_id=captured.owner_id))):
            raise HarnessSelectionError("harness_config_changed")
        self._current()

    @staticmethod
    def assert_connection_origin(origin):
        if origin is not None:
            from orchestrator.harness.host_admission import ConnectionOrigin
            if type(origin) is not ConnectionOrigin:
                raise HarnessSelectionError("harness_owner_invalid", 403)
            origin.assert_current()

    @classmethod
    def assert_origin(cls, admission):
        if admission is not None:
            cls.assert_connection_origin(admission.origin)


async def select_captured_config(capture, *, store, owner_id):
    fact = deployment_for_capture(capture)
    if capture.owner_id != owner_id:
        raise HarnessSelectionError("harness_owner_invalid", 403)
    key = private_binding_key(capture.config_binding_key_id)
    selected = _selected_config(fact, await store.capture_user(owner_id), store, key)
    if selected.revision != capture.config_revision:
        raise HarnessSelectionError("harness_config_changed")
    return selected, key
