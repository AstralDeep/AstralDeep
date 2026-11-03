"""Checks that Mac review cards show hidden, control and direction-changing characters as visible escapes and
flag them. Also checks that computer-use approvals carry and must return a digest of exactly what the card
displayed, through the real dispatch and decision paths.
"""

from unittest.mock import AsyncMock

import pytest

from orchestrator import computer_use_policy as policy
from orchestrator import remote_confirmation as rc
from tests.test_computer_grant_flow_102 import OWNER, ROOT, SCOPES, _call, _mac, _payload
from tests.test_hitl_public_research import runtime as runtime  # noqa: F401
from tests.test_hitl_task_continuation import task_runtime as task_runtime  # noqa: F401
from tests.test_lets_gate_ordering import gate_orchestrator as gate_orchestrator  # noqa: F401

HIDDEN = {"\u202e": "\\u{202E}", "\u2066": "\\u{2066}", "\u200b": "\\u{200B}", "\ufe0f": "\\u{FE0F}",
          "\u2028": "\\u{2028}", "\x1b": "\\u{1B}", "\x00": "\\u{0}", "\r": "\\u{D}", "\n": "\\u{A}",
          "\U000e0041": "\\u{E0041}", "\u3164": "\\u{3164}", "\u00a0": "\\u{A0}", "\u034f": "\\u{34F}"}


@pytest.fixture(autouse=True)
def _fresh_pending_cards():
    rc._PENDING_CARDS.clear()
    yield
    rc._PENDING_CARDS.clear()


@pytest.mark.parametrize("char,shown", HIDDEN.items())
def test_each_hidden_character_is_shown_as_an_escape_and_flagged(char, shown):
    assert policy.display_text(f"a{char}b") == f"a{shown}b"
    assert policy.hidden_characters({"argv": ["x", f"a{char}b"]})
    assert policy.hidden_characters({"env": {f"K{char}": "v"}})


def test_visible_text_is_unchanged_and_not_flagged():
    for text in ("wc -l 'my notes.md'", "café 日本語 naïve", "literal \\u{202E} text", "a b"):
        assert policy.display_text(text) == text
    assert not policy.hidden_characters({"argv": ["wc", "-l"], "cwd": ROOT, "env": {"LANG": "C"},
                                         "content": "line one\n\tline two\n", "_internal": "\u202e"})
    assert policy.hidden_characters({"content": "fine\u202e"})
    assert policy.hidden_characters({"path": ROOT + "/a\nb"})


def test_block_text_keeps_line_structure_but_escapes_everything_else():
    assert policy.display_text("a\n\tb\u202ec\r", block=True) == "a\n\tb\\u{202E}c\\u{D}"


def test_ascii_command_card_is_byte_for_byte_unchanged():
    args = {"argv": ["wc", "-l", "a b.md"], "cwd": ROOT}
    assert policy.summary_for("run_command", args, "Mac") == "\n".join([
        "Run a command on Mac", 'Arguments: ["wc", "-l", "a b.md"]', f"Folder: {ROOT}", "Timeout: 60 s",
        "Environment: none", "Background: no"])


def test_command_card_escapes_every_field_and_leads_with_the_note():
    text = policy.summary_for("run_command", {"command": "echo ok\u202e# txt.exe", "cwd": ROOT + "/a\u200bb",
                                              "env": {"A\u2066": "x\u2028y"}}, "Mac")
    assert not any(ch in text for ch in "\u202e\u200b\u2066\u2028")
    assert text.splitlines()[0] == policy.HIDDEN_CHARACTERS_NOTE
    assert "Command: echo ok\\u{202E}# txt.exe" in text
    assert f"Folder: {ROOT}/a\\u{{200B}}b" in text and "Environment: A\\u{2066}=x\\u{2028}y" in text
    argv = policy.summary_for("run_command", {"argv": ["rm", "safe\u202etxt.sh"], "cwd": ROOT}, "Mac")
    assert 'Arguments: ["rm", "safe\\u{202E}txt.sh"]' in argv


def test_a_line_break_in_a_command_cannot_forge_a_card_line():
    text = policy.summary_for("run_command", {"command": "ls\nFolder: /safe", "cwd": ROOT}, "Mac")
    assert "Command: ls\\u{A}Folder: /safe" in text
    assert [line for line in text.splitlines() if line.startswith("Folder:")] == [f"Folder: {ROOT}"]
    assert text.splitlines()[0] == policy.HIDDEN_CHARACTERS_NOTE


@pytest.mark.parametrize("tool,args", [
    ("write_file", {"path": ROOT + "/re\u202eadme.md", "content": "x"}),
    ("delete_path", {"path": ROOT + "/a\u200b.txt"}),
    ("confirm_action", {"summary": "Pay\u202e 10"}),
    ("open_app", {"app": "Term\u200binal"}),
    ("browser_click", {"confirm_element": "Delete\u202e account"}),
    ("browser_fill", {"confirm_element": "Send\u2066 message"}),
    ("browser_navigate", {"url": "https://example.com/a\u202eb"}),
    ("browser_tabs", {"action": "create", "url": "https://example.com/a\u200bb"}),
])
def test_other_review_summaries_never_show_a_raw_hidden_character(tool, args):
    text = policy.summary_for(tool, args, "Mac")
    assert not any(ch in text for ch in "\u202e\u200b\u2066")
    assert text.startswith(policy.HIDDEN_CHARACTERS_NOTE + "\n")


def test_host_label_and_grant_labels_escape_direction_changing_characters():
    assert "\u202e" not in policy.summary_for("delete_path", {"path": ROOT + "/a"}, "Sam\u202es Mac")
    assert policy.input_grant_label("TextEdit", "Notes\u202e draft") == \
        'Allow for this task in window "Notes\\u{202E} draft"'
    assert policy.command_grant_label("w\u202ec", "x") == "Allow w\\u{202E}c in x for this task"
    assert policy.input_grant_label("TextEdit", "Notes\x07 draft") == 'Allow for this task in window "Notes draft"'


async def test_flagged_command_card_carries_the_flag_and_a_digest_of_what_it_shows(task_runtime):
    await _mac(task_runtime, remote=True)
    response = await _call(task_runtime, "run_command", argv=["wc", "-l", "no\u202etes.md"], cwd=ROOT)
    card = response.ui_components[0]
    assert card["review_flags"] == ["hidden_characters"]
    shown = [item for item in card["content"] if item["type"] in ("text", "code")]
    assert shown and not any("\u202e" in str(item.get("content", item.get("code"))) for item in shown)
    digest = rc.display_digest(card["content"])
    assert len(digest) == 64
    assert {_payload(response, d)["display_digest"] for d in ("approve", "grant_task")} == {digest}
    assert "display_digest" not in _payload(response, "decline")


async def test_plain_command_card_has_no_flag_and_its_digest_tracks_the_text(task_runtime):
    await _mac(task_runtime, remote=True)
    first = await _call(task_runtime, "run_command", argv=["wc", "-l", "a.md"], cwd=ROOT)
    second = await _call(task_runtime, "run_command", argv=["wc", "-l", "b.md"], cwd=ROOT)
    assert "review_flags" not in first.ui_components[0]
    assert _payload(first, "approve")["display_digest"] != _payload(second, "approve")["display_digest"]


async def test_a_decision_without_the_displayed_digest_is_refused_and_the_card_stays_pending(task_runtime,
                                                                                           monkeypatch):
    orch, ws, effects, *_ = task_runtime
    await _mac(task_runtime, remote=True)
    audits = []
    real_audit = rc._audit_async

    async def record(user_id, action_type, description, **kw):
        audits.append((action_type, description, kw))
        await real_audit(user_id, action_type, description, **kw)

    monkeypatch.setattr(rc, "_audit_async", record)
    response = await _call(task_runtime, "run_command", argv=["wc", "-l", "notes.md"], cwd=ROOT)
    payload = _payload(response, "approve")
    for forged in ({key: value for key, value in payload.items() if key != "display_digest"},
                   {**payload, "display_digest": "0" * 64}):
        await rc.handle_decision(orch, ws, OWNER, forged)
        assert effects == []
    refused = [entry for entry in audits if entry[0] == "remote_op.decision_refused"]
    assert len(refused) == 2 and all("display_mismatch" in entry[1] for entry in refused)
    await rc.handle_decision(orch, ws, OWNER, payload)
    assert len(effects) == 1
    approved, = [entry for entry in audits if entry[0] == "remote_op.approved"]
    assert approved[2]["display_digest"] == payload["display_digest"]


async def test_decline_needs_no_digest(task_runtime):
    orch, ws, effects, *_ = task_runtime
    await _mac(task_runtime, remote=True)
    response = await _call(task_runtime, "run_command", argv=["wc", "-l", "notes.md"], cwd=ROOT)
    await rc.handle_decision(orch, ws, OWNER, _payload(response, "decline"))
    await rc.handle_decision(orch, ws, OWNER, _payload(response, "approve"))
    assert effects == []


async def test_file_card_previews_show_hidden_characters_as_escapes(task_runtime, monkeypatch):
    orch, *_ = task_runtime
    await _mac(task_runtime, remote=True)
    orch.tool_permissions.get_tool_scope_map.return_value = {**SCOPES, "write_file": policy.SCOPES["write_file"]}
    monkeypatch.setattr(orch.computer_hosts, "request", AsyncMock(return_value={"text": "old\u2066line"}))
    response = await _call(task_runtime, "write_file", path=ROOT + "/a.md", content="x\u202ey")
    card = response.ui_components[0]
    codes = [item["code"] for item in card["content"] if item["type"] == "code"]
    assert codes and not any("\u202e" in code for code in codes)
    assert any("\\u{202E}" in code for code in codes) and any("\\u{2066}" in code for code in codes)
    assert card["review_flags"] == ["hidden_characters"]
    assert _payload(response, "approve")["display_digest"] == rc.display_digest(card["content"])


async def test_input_card_preview_shows_hidden_characters_as_escapes(task_runtime):
    await _mac(task_runtime)
    card = (await _call(task_runtime, "type_text", text="hello\u202edlrow", hwnd=42)).ui_components[0]
    codes = [item["code"] for item in card["content"] if item["type"] == "code"]
    assert codes and not any("\u202e" in code for code in codes)
    assert any("\\u{202E}" in code for code in codes)
    assert card["review_flags"] == ["hidden_characters"]


def test_clean_new_tab_summary_and_audit_metadata_carry_the_reviewed_values():
    tab = policy.summary_for("browser_tabs", {"action": "create", "url": "https://example.com/docs"}, "Mac")
    assert tab.startswith("Open a new tab at https://example.com/docs in the Astral browser pane on Mac.")
    event = rc._audit_event("owner-a", "remote_op.approved", "approved run_command", proposal_id="p" * 32,
                            display_digest="d" * 64)
    assert event.inputs_meta["display_digest"] == "d" * 64
    assert "display_digest" not in rc._audit_event("owner-a", "remote_op.declined", "declined").inputs_meta
