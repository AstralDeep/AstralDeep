"""Source-contract guard for the feature-060 web continuity reducer.

The deterministic executable behavior is covered in
``tooling/web-ci/tests/continuity-contract-060.spec.js`` with a real Chromium
DOM and a synthetic transport. The separate release harness uses real
Keycloak and WebSocket transport. These assertions keep the shipped classic
script's security- and ordering-critical seams discoverable from the normal
backend suite, including when the browser producer is unavailable locally.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
CLIENT = ROOT / "backend" / "webrender" / "static" / "client.js"
BROWSER_SPEC = (
    ROOT / "tooling" / "web-ci" / "tests" / "continuity-contract-060.spec.js"
)


@pytest.fixture(scope="module")
def client_source() -> str:
    return CLIENT.read_text(encoding="utf-8")


def _function(source: str, name: str) -> str:
    signature = f"function {name}("
    start = source.index(signature)
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unbalanced JavaScript function {name}")


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value)


def test_locator_is_documented_account_scoped_and_non_credential(client_source: str) -> None:
    assert "astraldeep.active_chat.v1." in client_source
    digest = _normalized(_function(client_source, "activeChatStorageKey"))
    assert "issuer" in digest and "subject" in digest
    assert "TextEncoder" in digest and "crypto.subtle.digest" in digest
    assert '"SHA-256"' in digest

    persist = _normalized(_function(client_source, "persistActiveChatLocator"))
    assert "schema_version: 1" in persist
    assert "chat_id:" in persist and "updated_at:" in persist
    assert "localStorage.setItem" in persist
    for forbidden in ("token", "transcript", "canvas", "provider"):
        assert forbidden not in persist.lower()


def test_locator_is_bound_before_registration_and_survives_disconnect(
    client_source: str,
) -> None:
    registration = _normalized(_function(client_source, "sendRegistration"))
    assert registration.index("persistActiveChatLocator") < registration.index("send(")
    assert "connection_generation" in registration
    assert "request_generation" in registration
    assert "resume" in registration

    connect = _normalized(_function(client_source, "connect"))
    onclose = connect[connect.index("ws.onclose") :]
    assert "clearActiveChatLocator" not in onclose
    assert "localStorage.removeItem" not in onclose


def test_only_four_definitive_locator_clear_reasons_are_accepted(
    client_source: str,
) -> None:
    clear = _normalized(_function(client_source, "clearActiveChatLocator"))
    assert all(
        reason in clear
        for reason in (
            "explicit_new_chat",
            "definitive_sign_out",
            "account_switch",
            "confirmed_deletion",
        )
    )
    assert "ALLOWED_LOCATOR_CLEAR_REASONS" in clear
    assert "localStorage.removeItem" in clear


def test_shared_tab_account_switch_cannot_reuse_prior_principal_token(
    client_source: str,
) -> None:
    bootstrap = re.search(r"var token = ([^;]+);", client_source)
    assert bootstrap is not None
    expression = bootstrap.group(1)
    assert expression.index("window.__ASTRAL_TOKEN__") < expression.index(
        "sessionStorage.getItem(TOKEN_KEY)"
    )
    logout_start = client_source.index(
        "The local endpoint invalidates the server session"
    )
    logout_end = client_source.index("// ---- stacked-shell chrome", logout_start)
    logout = client_source[logout_start:logout_end]
    assert "sessionStorage.removeItem(TOKEN_KEY)" in logout
    assert "sessionStorage.removeItem(ACCOUNT_SESSION_KEY)" in logout


def test_snapshot_candidate_requires_exact_web_presentation_envelope(
    client_source: str,
) -> None:
    presentation = _normalized(_function(client_source, "prepareWebPresentation"))
    assert "_presentation" in presentation
    assert 'target !== "web"' in presentation
    assert 'typeof envelope.html !== "string"' in presentation
    assert "workspace.export" in presentation
    assert "workspace.share" in presentation
    assert "childElementCount !== 1" in presentation
    assert "rendered_html" not in presentation

    semantic = _normalized(_function(client_source, "semanticClone"))
    assert 'key === "_presentation"' in semantic


def test_snapshot_reducer_is_purpose_aware_and_atomic(client_source: str) -> None:
    reducer = _normalized(_function(client_source, "reduceConversationSnapshot"))
    for disposition in (
        "wrong_scope",
        "wrong_purpose",
        "stale_frame_ignored",
        "unexpected_equal_commit",
        "snapshot_replay",
        "revision_conflict",
        "snapshot_applied",
    ):
        assert disposition in reducer
    assert "requestState.purpose" in reducer
    assert "requestState.hydrationApplied" in reducer
    assert "requestState.acceptedSnapshotId" in reducer
    assert "commitSnapshotCandidate" in reducer

    commit = _normalized(_function(client_source, "commitSnapshotCandidate"))
    assert "chat.replaceChildren" in commit
    assert "canvas.replaceChildren" in commit
    assert "lastCommittedRenderRevision" in commit
    assert commit.index("candidate") < commit.index("chat.replaceChildren")


def test_transient_reducer_is_request_scoped_and_never_advances_commit(
    client_source: str,
) -> None:
    accept = _normalized(_function(client_source, "acceptTransientFrame"))
    assert "base_render_revision" in accept
    assert "frame_sequence" in accept
    assert "requestState.lastFrameSequence" in accept
    assert "lastCommittedRenderRevision" in accept

    reducer = _normalized(_function(client_source, "reduceTransientFrame"))
    assert "ensureTransientOverlay" in reducer
    assert "committedTranscript" not in reducer
    assert "lastCommittedRenderRevision =" not in reducer


def test_server_originated_commit_prelude_opens_only_an_exact_fresh_fence(
    client_source: str,
) -> None:
    reducer = _normalized(
        _function(client_source, "acceptConversationCommitReady")
    )
    for token in (
        "exactKeys",
        "conversation_commit_ready",
        "schema_version",
        "connectionGeneration",
        "lastCommittedRenderRevision",
        "commit_request_busy",
        "stale_commit_ready",
        "openRequest(\"commit\"",
        "frame.request_generation",
    ):
        assert token in reducer
    assert "persistActiveChatLocator" not in reducer


def test_semantic_decoder_keeps_every_canonical_part_visible(
    client_source: str,
) -> None:
    decoder = _normalized(_function(client_source, "decodeSemanticMessage"))
    for part_type in ("text", "components", "structured", "recovery"):
        assert f'part.type === "{part_type}"' in decoder
    assert "plain_text" in decoder
    assert "data-structured-value" in decoder
    assert "role" in decoder and "alert" in decoder


@pytest.mark.skipif(
    not BROWSER_SPEC.is_file(),  # web tooling is not mounted in the product-image lane
    reason="the browser contract is not present in this isolated test lane",
)
def test_playwright_contract_exercises_runtime_not_only_source_text() -> None:
    source = BROWSER_SPEC.read_text(encoding="utf-8")
    assert "FakeWebSocket" in source
    assert "conversation_snapshot" in source
    assert "_presentation" in source
    assert "transient overlay" in source.lower()
    assert "explicit new chat" in source.lower()
    assert "page.evaluate" in source
