"""Checks bounded generated answer views without granting their text or identifiers authority.
Synthetic clocks and coordinated threads cover exact content, source dependencies, isolation, expiry, and capacity.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone, tzinfo
import hashlib
import math
import re
from threading import Barrier

import pytest

from orchestrator import context_views as module
from orchestrator.context_views import (
    ContextViewStore, MAX_CONVERSATION_BYTES, MAX_CONVERSATION_RECORDS,
    MAX_DEPENDENCIES, MAX_GLOBAL_BYTES, MAX_RECORDS, MAX_VIEW_BYTES,
    SourceDependency, ViewCaptureError, ViewDenied, ViewLimit, ViewUnavailable,
)


NOW = datetime(2026, 10, 8, 16, tzinfo=UTC)
BINDINGS = {"owner_id": "owner-a", "conversation_id": "chat-a", "audience_id": "user:owner-a"}
TEXT = "Beginning 🙂€中 e\u0301\r\n\x00middle\u2028ending"


class Clock:
    def __init__(self):
        self.now = NOW
        self.elapsed = 0.0

    def __call__(self):
        return self.now

    def monotonic(self):
        return self.elapsed

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        self.elapsed += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def dependency():
    return SourceDependency("obs_" + "x" * 43, "a" * 64, "b" * 64, NOW + timedelta(minutes=20))


@pytest.fixture
def store(clock):
    return ContextViewStore(clock=clock, monotonic_clock=clock.monotonic)


def capture(store, dependency, text=TEXT, **overrides):
    return store.capture(text, **(BINDINGS | {"dependencies": (dependency,)} | overrides))


def read(store, view, dependencies=None, **overrides):
    return store.read(view.reference, **(BINDINGS | {
        "dependencies": view.dependencies if dependencies is None else dependencies,
    } | overrides))


def test_exact_literal_content_and_immutable_private_metadata(store, dependency):
    dependencies = [dependency]
    view = capture(store, dependency, dependencies=dependencies)
    dependencies.clear()
    assert re.fullmatch(r"view_[A-Za-z0-9_-]{43}", view.reference)
    assert view.dependencies == (dependency,) and view.size_bytes == len(TEXT.encode())
    assert view.digest == hashlib.sha256(TEXT.encode()).hexdigest()
    assert view.captured_at == NOW and view.expires_at == NOW + timedelta(minutes=10)
    assert store.inspect(view.reference, **BINDINGS) is view
    content = read(store, view)
    assert content.text == TEXT and content.reference == view.reference and content.digest == view.digest
    assert content.expires_at == view.expires_at and content.dependencies == view.dependencies
    assert content.to_dict() == {"reference": view.reference, "digest": view.digest,
        "text": TEXT, "size_bytes": view.size_bytes, "expires_at": view.expires_at.isoformat()}
    assert TEXT not in repr(view) and TEXT not in repr(content)
    assert not hasattr(view, "text") and not hasattr(view, "content")
    assert not {"owner_id", "conversation_id", "audience_id", "dependencies"} & content.to_dict().keys()
    for value, field in ((view, "digest"), (content, "text"), (dependency, "reference")):
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, "forged")


@pytest.mark.parametrize("text", ["", "plain", "🙂", "\r\n", "e\u0301", "中€🙂"])
def test_empty_and_unicode_views_are_exact(store, dependency, text):
    view = capture(store, dependency, text)
    assert read(store, view).text == text
    assert store.retained_bytes == len(text.encode()) and store.view_count == 1


def test_views_without_source_references_use_only_fixed_lifetime(store):
    view = store.capture(TEXT, **BINDINGS, dependencies=())
    assert view.dependencies == () and view.expires_at == NOW + timedelta(minutes=10)
    assert read(store, view).text == TEXT


@pytest.mark.parametrize("binding", BINDINGS)
def test_foreign_scope_and_unknown_reference_are_uniform(store, dependency, binding):
    view = capture(store, dependency)
    errors = []
    for reference, bindings in [(view.reference, BINDINGS | {binding: "foreign"}), ("view_" + "z" * 43, BINDINGS)]:
        for operation in (store.inspect, store.delete):
            with pytest.raises(ViewDenied) as error:
                operation(reference, **bindings)
            errors.append(str(error.value))
        with pytest.raises(ViewDenied) as error:
            store.read(reference, **bindings, dependencies=view.dependencies)
        errors.append(str(error.value))
    assert len(set(errors)) == 1 and not any(value in errors[0] for value in BINDINGS.values())
    assert store.view_count == 1 and read(store, view).text == TEXT


@pytest.mark.parametrize("reference", [None, 0, [], {}, "", "view_", "view_" + "x" * 44, "obs_" + "x" * 43])
def test_invalid_references_disclose_nothing(store, reference):
    with pytest.raises(ViewDenied):
        store.inspect(reference, **BINDINGS)


@pytest.mark.parametrize("value", [None, 0, [], "", "   ", "owner\n", "\x7f", "x" * 513, "🙂" * 129, "\ud800"])
def test_invalid_bindings_cannot_capture_or_access(store, dependency, value):
    with pytest.raises(ViewCaptureError):
        capture(store, dependency, owner_id=value)
    with pytest.raises(ViewDenied):
        store.inspect("view_" + "x" * 43, **(BINDINGS | {"owner_id": value}))
    assert store.view_count == 0


@pytest.mark.parametrize("text", [None, 0, True, [], {}, b"text", "\ud800"])
def test_unsupported_content_never_creates_a_view(store, dependency, text):
    with pytest.raises(ViewCaptureError):
        capture(store, dependency, text)
    assert store.view_count == 0 and store.retained_bytes == 0


def test_view_limit_counts_utf8_bytes_and_keeps_live_data(store, dependency):
    view = capture(store, dependency, "🙂" * (MAX_VIEW_BYTES // 4))
    assert view.size_bytes == MAX_VIEW_BYTES
    for text in ("x" * (MAX_VIEW_BYTES + 1), "🙂" * (MAX_VIEW_BYTES // 4) + "x"):
        with pytest.raises(ViewLimit):
            capture(store, dependency, text)
    assert read(store, view).text.encode() == "🙂".encode() * (MAX_VIEW_BYTES // 4)


def test_optional_digest_guard_binds_capture_inspect_and_read(store, dependency):
    digest = hashlib.sha256(TEXT.encode()).hexdigest()
    view = capture(store, dependency, expected_digest=digest)
    assert store.inspect(view.reference, **BINDINGS, expected_digest=digest) == view
    assert read(store, view, expected_digest=digest).text == TEXT
    for bad in ("0" * 64, "a" * 63, "A" * 64, 1, b"a" * 64):
        with pytest.raises(ViewCaptureError):
            capture(store, dependency, expected_digest=bad)
        with pytest.raises(ViewDenied):
            store.inspect(view.reference, **BINDINGS, expected_digest=bad)
        with pytest.raises(ViewDenied):
            read(store, view, expected_digest=bad)
    assert store.view_count == 1 and read(store, view).text == TEXT


@pytest.mark.parametrize("field,value", [
    ("reference", "obs_invalid"), ("reference", None),
    ("integrity_identity", "a" * 63), ("integrity_identity", "A" * 64),
    ("grant_fingerprint", 1), ("grant_fingerprint", "z" * 64),
    ("expires_at", NOW.replace(tzinfo=None)), ("expires_at", None),
])
def test_dependencies_require_exact_bounded_source_identity(dependency, field, value):
    with pytest.raises(ViewCaptureError):
        replace(dependency, **{field: value})


def test_dependency_timezone_is_normalized_without_changing_deadline(store, dependency):
    offset = timezone(timedelta(hours=2))
    dependency = replace(dependency, expires_at=(NOW + timedelta(seconds=10)).astimezone(offset))
    assert dependency.expires_at == NOW + timedelta(seconds=10) and dependency.expires_at.tzinfo is UTC
    assert capture(store, dependency).expires_at == dependency.expires_at


@pytest.mark.parametrize("dependencies", [None, {}, "sources", iter(()), [object()]])
def test_unbounded_or_untyped_dependencies_are_refused(store, dependency, dependencies):
    with pytest.raises(ViewCaptureError):
        capture(store, dependency, dependencies=dependencies)
    assert store.view_count == 0


def test_dependency_count_duplicates_and_expired_deadlines_refuse_capture(store, dependency):
    with pytest.raises(ViewCaptureError):
        capture(store, dependency, dependencies=(dependency, dependency))
    dependencies = tuple(replace(dependency, reference="obs_" + f"{index:043d}") for index in range(MAX_DEPENDENCIES + 1))
    with pytest.raises(ViewCaptureError):
        capture(store, dependency, dependencies=dependencies)
    with pytest.raises(ViewCaptureError):
        capture(store, replace(dependency, expires_at=NOW))
    assert store.view_count == 0


@pytest.mark.parametrize("change", ["missing", "extra", "order", "integrity", "grant", "expiry", "untyped"])
def test_current_dependencies_must_match_exactly_before_read(store, dependency, change):
    other = replace(dependency, reference="obs_" + "y" * 43)
    view = capture(store, dependency, dependencies=(dependency, other))
    changed = {
        "missing": (), "extra": (dependency, other, replace(dependency, reference="obs_" + "z" * 43)),
        "order": (other, dependency), "integrity": (replace(dependency, integrity_identity="c" * 64), other),
        "grant": (replace(dependency, grant_fingerprint="c" * 64), other),
        "expiry": (replace(dependency, expires_at=dependency.expires_at + timedelta(seconds=1)), other),
        "untyped": [{"reference": dependency.reference}],
    }[change]
    with pytest.raises(ViewDenied):
        read(store, view, changed)
    with pytest.raises(ViewDenied):
        store.inspect(view.reference, **BINDINGS, dependencies=changed)
    assert read(store, view).text == TEXT and store.view_count == 1


def test_read_requires_dependency_proof_and_inspect_can_discover_metadata(store, dependency):
    view = capture(store, dependency)
    assert store.inspect(view.reference, **BINDINGS).dependencies == (dependency,)
    with pytest.raises(TypeError):
        store.read(view.reference, **BINDINGS)
    with pytest.raises(ViewDenied):
        read(store, view, dependencies={})
    with pytest.raises(ViewDenied):
        store.read(view.reference, **BINDINGS, dependencies=None)


@pytest.mark.parametrize("value", [[], {}, None])
def test_tampered_dependency_reference_is_refused_without_breaking_denial(store, dependency, value):
    view = capture(store, dependency)
    forged = replace(dependency)
    object.__setattr__(forged, "reference", value)
    with pytest.raises(ViewCaptureError):
        capture(store, dependency, dependencies=(forged,))
    with pytest.raises(ViewDenied):
        read(store, view, dependencies=(forged,))
    assert read(store, view).text == TEXT


def test_incomplete_source_metadata_has_only_stable_refusal_errors(store, dependency):
    forged = object.__new__(SourceDependency)
    view = capture(store, dependency)
    with pytest.raises(ViewCaptureError):
        capture(store, dependency, dependencies=(forged,))
    with pytest.raises(ViewDenied):
        read(store, view, dependencies=(forged,))
    assert read(store, view).text == TEXT


@pytest.mark.parametrize("deadline", [1, 300, 600, 1200])
def test_fixed_and_every_earlier_dependency_expiry_never_slide(store, dependency, clock, deadline):
    dependency = replace(dependency, expires_at=NOW + timedelta(seconds=deadline))
    view = capture(store, dependency)
    expected = min(deadline, 600)
    assert view.expires_at == NOW + timedelta(seconds=expected)
    clock.advance(expected - 1)
    assert read(store, view).text == TEXT and view.expires_at == NOW + timedelta(seconds=expected)
    clock.advance(1)
    with pytest.raises(ViewUnavailable, match="expired"):
        read(store, view)
    assert store.retained_bytes == 0 and store.view_count == 0
    with pytest.raises(ViewDenied):
        store.inspect(view.reference, **(BINDINGS | {"owner_id": "foreign"}))


def test_multiple_dependencies_apply_the_earliest_deadline(store, dependency):
    earlier = replace(dependency, reference="obs_" + "y" * 43, expires_at=NOW + timedelta(seconds=7))
    assert capture(store, dependency, dependencies=(dependency, earlier)).expires_at == earlier.expires_at


def test_monotonic_expiry_applies_when_wall_clock_stops(store, dependency, clock):
    view = capture(store, dependency)
    clock.elapsed = 600
    with pytest.raises(ViewUnavailable, match="expired"):
        read(store, view)


@pytest.mark.parametrize("clock_kind", ["wall", "monotonic"])
def test_backward_clock_destroys_text_and_cannot_recover_without_reset(store, dependency, clock, clock_kind):
    first, second = capture(store, dependency), capture(store, dependency)
    clock.advance(30)
    assert read(store, first).text == TEXT
    if clock_kind == "wall":
        clock.now -= timedelta(seconds=1)
    else:
        clock.elapsed -= 1
    with pytest.raises(ViewUnavailable, match="clock_unavailable"):
        read(store, first)
    assert store.retained_bytes == store.view_count == 0
    clock.advance(60)
    with pytest.raises(ViewUnavailable, match="clock_unavailable"):
        capture(store, dependency)
    with pytest.raises(ViewUnavailable, match="clock_unavailable"):
        read(store, second)
    with pytest.raises(ViewDenied):
        store.inspect(first.reference, **(BINDINGS | {"owner_id": "foreign"}))
    assert store.clear() == ()


@pytest.mark.parametrize("kind,value", [
    ("wall", None), ("wall", NOW.replace(tzinfo=None)),
    ("monotonic", None), ("monotonic", True), ("monotonic", "1"),
    ("monotonic", math.nan), ("monotonic", math.inf), ("monotonic", -1),
])
def test_invalid_clock_fails_closed_and_removes_all_text(store, dependency, clock, kind, value):
    view = capture(store, dependency)
    if kind == "wall":
        clock.now = value
    else:
        clock.elapsed = value
    with pytest.raises(ViewUnavailable, match="clock_unavailable"):
        read(store, view)
    assert store.retained_bytes == store.view_count == 0


def test_clock_exceptions_and_unrepresentable_expiry_are_safe(dependency):
    def broken():
        raise RuntimeError("secret clock diagnostic")
    for store in (
        ContextViewStore(clock=broken),
        ContextViewStore(clock=lambda: NOW, monotonic_clock=lambda: 1e308),
        ContextViewStore(clock=lambda: datetime.max.replace(tzinfo=UTC), monotonic_clock=lambda: 0.0),
    ):
        with pytest.raises(ViewUnavailable) as error:
            store.capture(TEXT, **BINDINGS, dependencies=())
        assert str(error.value) == "context_view_clock_unavailable" and "secret" not in str(error.value)
        assert store.retained_bytes == store.view_count == 0


@pytest.mark.parametrize("tamper", ["content", "digest", "size", "expiry", "scope", "dependencies", "identity", "type"])
def test_integrity_failure_removes_text_without_adopting_changed_scope(store, dependency, tamper):
    view = capture(store, dependency)
    stored = store._records[view.reference]
    if tamper == "content":
        stored.content = b"forged content"
    elif tamper == "type":
        stored.metadata = object()
    else:
        field, value = {
            "digest": ("digest", "c" * 64), "size": ("size_bytes", 1),
            "expiry": ("expires_at", NOW + timedelta(hours=1)), "scope": ("owner_id", "foreign"),
            "dependencies": ("dependencies", ()), "identity": ("integrity_identity", "c" * 64),
        }[tamper]
        object.__setattr__(view, field, value)
    with pytest.raises(ViewUnavailable, match="integrity_failed"):
        store.read(view.reference, **BINDINGS, dependencies=(dependency,))
    assert store.view_count == store.retained_bytes == 0
    assert stored.content == b""
    with pytest.raises(ViewDenied):
        store.inspect(view.reference, **(BINDINGS | {"owner_id": "foreign"}))


def test_source_metadata_tampering_before_capture_is_not_trusted(store, dependency):
    object.__setattr__(dependency, "grant_fingerprint", "not a digest")
    with pytest.raises(ViewCaptureError):
        capture(store, dependency)
    assert store.view_count == 0


def test_per_conversation_and_global_byte_capacity_never_evict_live_views(clock, dependency):
    store = ContextViewStore(clock=clock, monotonic_clock=clock.monotonic,
        view_limit_bytes=4, conversation_limit_bytes=4, global_limit_bytes=8)
    first = capture(store, dependency, "1234")
    with pytest.raises(ViewLimit):
        capture(store, dependency, "x", audience_id="agent:reader")
    second = capture(store, dependency, "5678", conversation_id="chat-b")
    with pytest.raises(ViewLimit):
        capture(store, dependency, "x", conversation_id="chat-c")
    assert read(store, first).text == "1234"
    assert read(store, second, conversation_id="chat-b").text == "5678"
    assert store.retained_bytes == 8 and store.view_count == 2


def test_record_limits_bound_empty_views_and_share_conversation_capacity(clock, dependency):
    store = ContextViewStore(clock=clock, monotonic_clock=clock.monotonic,
        conversation_max_records=2, max_records=3)
    first, second = capture(store, dependency, ""), capture(store, dependency, "", audience_id="agent:reader")
    with pytest.raises(ViewLimit):
        capture(store, dependency, "")
    third = capture(store, dependency, "", owner_id="owner-b")
    with pytest.raises(ViewLimit):
        capture(store, dependency, "", conversation_id="chat-b")
    assert store.retained_bytes == 0 and store.view_count == 3
    assert store.delete(first.reference, **BINDINGS) == first
    assert capture(store, dependency, "").reference not in {first.reference, second.reference, third.reference}


def test_capture_reclaims_only_expired_capacity(clock, dependency):
    store = ContextViewStore(clock=clock, monotonic_clock=clock.monotonic, max_records=1)
    first = capture(store, replace(dependency, expires_at=NOW + timedelta(seconds=1)))
    clock.advance(1)
    second = capture(store, dependency)
    with pytest.raises(ViewUnavailable, match="expired"):
        read(store, first)
    assert read(store, second).text == TEXT and store.view_count == 1


def test_cleanup_and_clear_release_bytes_and_return_exact_metadata(store, dependency, clock):
    first = capture(store, replace(dependency, expires_at=NOW + timedelta(seconds=1)))
    second = capture(store, dependency)
    clock.advance(1)
    assert store.cleanup() == (first,) and store.cleanup() == ()
    assert store.clear() == (second,) and store.clear() == ()
    assert store.retained_bytes == store.view_count == 0
    with pytest.raises(ViewUnavailable, match="deleted"):
        read(store, second)


def test_sweep_uses_original_deadline_even_if_returned_metadata_is_tampered(store, dependency, clock):
    view = capture(store, replace(dependency, expires_at=NOW + timedelta(seconds=5)))
    object.__setattr__(view, "expires_at", NOW + timedelta(hours=1))
    clock.now += timedelta(seconds=5)
    assert store.cleanup() == (view,)
    assert store.retained_bytes == store.view_count == 0
    with pytest.raises(ViewUnavailable, match="expired"):
        read(store, view)


def test_source_revocation_uses_original_dependencies_even_if_returned_metadata_is_tampered(store, dependency):
    view = capture(store, dependency)
    object.__setattr__(view, "dependencies", ())
    assert store.revoke(owner_id=BINDINGS["owner_id"], source_reference=dependency.reference) == (view,)
    assert store.retained_bytes == store.view_count == 0
    with pytest.raises(ViewUnavailable, match="revoked"):
        read(store, view)


def test_revoke_cascades_only_matching_owner_chat_and_source(store, dependency):
    first = capture(store, dependency)
    second = capture(store, dependency, conversation_id="chat-b")
    unrelated = capture(store, replace(dependency, reference="obs_" + "y" * 43))
    other_owner = capture(store, dependency, owner_id="owner-b")
    assert store.revoke(owner_id="foreign", source_reference=dependency.reference) == ()
    assert store.revoke(owner_id=BINDINGS["owner_id"], conversation_id="chat-a", source_reference=dependency.reference) == (first,)
    assert store.revoke(owner_id=BINDINGS["owner_id"], source_reference=dependency.reference) == (second,)
    assert store.revoke(owner_id=BINDINGS["owner_id"]) == (unrelated,)
    assert read(store, other_owner, owner_id="owner-b").text == TEXT
    for view in (first, second, unrelated):
        with pytest.raises(ViewUnavailable, match="revoked"):
            read(store, view, conversation_id=view.conversation_id)


@pytest.mark.parametrize("overrides", [{"owner_id": None}, {"conversation_id": ""}, {"source_reference": "invalid"}])
def test_invalid_revocation_selector_does_not_remove_views(store, dependency, overrides):
    view = capture(store, dependency)
    with pytest.raises(ViewDenied):
        store.revoke(**({"owner_id": BINDINGS["owner_id"]} | overrides))
    assert read(store, view).text == TEXT


def test_tombstones_are_bounded_and_reset_does_not_reconstruct_text(clock, dependency):
    store = ContextViewStore(clock=clock, monotonic_clock=clock.monotonic, max_records=1)
    first = capture(store, dependency)
    store.delete(first.reference, **BINDINGS)
    second = capture(store, dependency)
    store.delete(second.reference, **BINDINGS)
    assert len(store._tombstones) == 1
    with pytest.raises(ViewDenied):
        read(store, first)
    with pytest.raises(ViewUnavailable, match="deleted"):
        read(store, second)
    reset = ContextViewStore(clock=clock, monotonic_clock=clock.monotonic)
    with pytest.raises(ViewDenied, match="unavailable_or_not_authorized"):
        read(reset, second)


@pytest.mark.parametrize("field,maximum", [
    ("view_limit_bytes", MAX_VIEW_BYTES), ("conversation_limit_bytes", MAX_CONVERSATION_BYTES),
    ("conversation_max_records", MAX_CONVERSATION_RECORDS), ("global_limit_bytes", MAX_GLOBAL_BYTES),
    ("max_records", MAX_RECORDS),
])
@pytest.mark.parametrize("kind", ["zero", "negative", "boolean", "floating", "unbounded"])
def test_limits_can_only_be_configured_downward(field, maximum, kind):
    value = {"zero": 0, "negative": -1, "boolean": True, "floating": 1.0, "unbounded": maximum + 1}[kind]
    with pytest.raises(ValueError, match="invalid_context_view_limit"):
        ContextViewStore(**{field: value})


@pytest.mark.parametrize("kwargs", [{"clock": 1}, {"monotonic_clock": 1}])
def test_clock_configuration_must_be_callable(kwargs):
    with pytest.raises(ValueError, match="invalid_context_view_clock"):
        ContextViewStore(**kwargs)


def test_random_id_collisions_never_overwrite_live_or_deleted_views(store, dependency, monkeypatch):
    monkeypatch.setattr(module.secrets, "token_urlsafe", lambda _length: "x" * 43)
    first = capture(store, dependency)
    with pytest.raises(ViewCaptureError):
        capture(store, dependency)
    assert read(store, first).text == TEXT
    store.delete(first.reference, **BINDINGS)
    with pytest.raises(ViewCaptureError):
        capture(store, dependency)
    assert store.retained_bytes == store.view_count == 0


def test_concurrent_capture_is_atomic(clock, dependency):
    store = ContextViewStore(clock=clock, monotonic_clock=clock.monotonic,
        view_limit_bytes=4, conversation_limit_bytes=4, global_limit_bytes=4)
    barrier = Barrier(2)
    def admitted(text):
        barrier.wait()
        try:
            return capture(store, dependency, text)
        except ViewLimit:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(admitted, ("1234", "5678")))
    assert sum(value is not None for value in results) == 1
    assert store.retained_bytes == 4 and store.view_count == 1
    assert read(store, next(value for value in results if value is not None)).text in {"1234", "5678"}


class BrokenTimezone(tzinfo):
    def utcoffset(self, _value):
        raise RuntimeError("secret timezone failure")


def test_broken_timezone_is_refused_without_diagnostic_source_data(dependency):
    with pytest.raises(ViewCaptureError) as error:
        replace(dependency, expires_at=NOW.replace(tzinfo=BrokenTimezone()))
    assert "secret" not in str(error.value)
