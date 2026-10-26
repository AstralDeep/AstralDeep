"""Tests for orchestrator/computer_grants.py: task grant creation, matching by application,
window, command folder and browser origin, refusal of never-grantable scopes, sliding and hard
expiry, replacement, revocation and the audit hook.
"""

from __future__ import annotations

import pytest

from orchestrator import computer_grants as cg
from orchestrator.computer_grants import GrantStore, TaskGrant

T0 = 1_800_000_000.0
BIND = {"owner_id": "owner-1", "host_id": "host-1", "chat_id": "chat-1", "task_id": "task-1"}
MATCH = {"owner_id": "owner-1", "host_id": "host-1", "chat_id": "chat-1"}
ORIGIN = {"kind": "origin", "origin": "https://example.org"}
APP = {"kind": "app", "bundle_id": "com.apple.TextEdit"}
WINDOW = {"kind": "window", "bundle_id": "com.apple.TextEdit", "hwnd": 4242}
ROOT = "/Users/sam/Projects/x"
CMD = {"argv0": "wc", "root": ROOT}


class _Events:
    def __init__(self):
        self.records: list = []

    def __call__(self, name, payload):
        self.records.append((name, payload))

    def names(self):
        return [name for name, _ in self.records]


def _store():
    events = _Events()
    return GrantStore(events=events), events


def _create(store, scope=APP, verb_classes=("input",), command=None, session="s-1", now=T0, **over):
    binding = {**BIND, **over}
    return store.create(binding["owner_id"], binding["host_id"], session, binding["chat_id"],
                        binding["task_id"], scope, verb_classes, command, now=now)


def _match(store, verb, target, session="s-1", now=T0 + 1, **over):
    return store.match(session, verb, target, now=now, **{**MATCH, **over})


def test_app_grant_covers_input_verbs_in_any_window_of_that_app_only():
    store, events = _store()
    grant = _create(store)
    assert isinstance(grant, TaskGrant)
    assert events.names() == [cg.EVENT_CREATED]
    for verb in sorted(cg.INPUT_GRANT_VERBS):
        assert _match(store, verb, {"bundle_id": "com.apple.TextEdit"}) == grant
    assert _match(store, "click", {"bundle_id": "com.apple.textedit", "hwnd": 9}) == grant
    assert _match(store, "click", {"bundle_id": "com.apple.Notes"}) is None
    assert _match(store, "click", {}) is None
    assert _match(store, "click", {"bundle_id": "com.apple.TextEdit", "hwnd": True}) is None
    assert _match(store, "run_command", {"argv0": "wc", "cwd": ROOT}) is None
    assert _match(store, "click", {"bundle_id": "com.apple.TextEdit"}, session="s-2") is None


def test_window_grant_needs_both_the_bundle_and_the_window():
    store, _ = _store()
    grant = _create(store, scope=WINDOW)
    assert _match(store, "type_text", {"bundle_id": "com.apple.TextEdit", "hwnd": 4242}) == grant
    assert _match(store, "type_text", {"bundle_id": "com.apple.TextEdit"}) is None
    assert _match(store, "type_text", {"bundle_id": "com.apple.TextEdit", "hwnd": 4243}) is None
    assert _match(store, "type_text", {"bundle_id": "com.apple.Notes", "hwnd": 4242}) is None
    assert _match(store, "type_text", {"hwnd": 4242}) is None


def test_command_grant_matches_the_program_inside_its_folder():
    store, _ = _store()
    grant = _create(store, scope={"kind": "command"}, verb_classes=("command",), command=CMD)
    for verb in ("run_command", "read_output", "interrupt_command"):
        assert _match(store, verb, {"argv0": "wc", "cwd": ROOT}) == grant
    assert _match(store, "run_command", {"argv0": "wc", "cwd": ROOT + "/notes/"}) == grant
    assert _match(store, "run_command", {"argv0": "wc", "cwd": ROOT + "/a/../b"}) == grant
    assert _match(store, "run_command", {"argv0": "cat", "cwd": ROOT}) is None
    assert _match(store, "run_command", {"argv0": "/usr/bin/wc", "cwd": ROOT}) is None
    assert _match(store, "run_command", {"argv0": "wc", "cwd": ROOT + "2"}) is None
    assert _match(store, "run_command", {"argv0": "wc", "cwd": "Projects/x"}) is None
    assert _match(store, "run_command", {"argv0": "wc"}) is None
    assert _match(store, "run_command", {"argv0": "wc", "cwd": ROOT + "\x00"}) is None
    assert _match(store, "click", {"bundle_id": "com.apple.TextEdit"}) is None


def test_cwd_escape_through_parent_segments_is_refused():
    store, _ = _store()
    _create(store, scope={"kind": "command"}, verb_classes=("command",), command=CMD)
    for cwd in (ROOT + "/..", ROOT + "/../y", ROOT + "/sub/../../y", "/Users/sam/Projects/x/../../..", "/"):
        assert _match(store, "run_command", {"argv0": "wc", "cwd": cwd}) is None


def test_app_grant_with_commands_covers_both_classes():
    store, _ = _store()
    grant = _create(store, verb_classes=("input", "command"), command=CMD)
    assert _match(store, "click", {"bundle_id": "com.apple.TextEdit"}) == grant
    assert _match(store, "run_command", {"argv0": "wc", "cwd": ROOT}) == grant


@pytest.mark.parametrize("bundle", [
    "com.apple.Terminal", "com.googlecode.iterm2", "com.mitchellh.ghostty", "dev.warp.Warp-Stable",
    "com.microsoft.VSCode", "com.jetbrains.pycharm", "com.todesktop.230313mzl4w4u92", "com.example.MyTerminal",
    "com.apple.Safari", "com.google.Chrome", "com.google.Chrome.canary", "org.mozilla.firefox",
    "company.thebrowser.Browser", "com.microsoft.edgemac", "com.brave.Browser", "com.operasoftware.Opera",
    "com.vivaldi.Vivaldi", "org.chromium.Chromium", "com.apple.mail", "com.apple.MobileSMS",
    "com.apple.FaceTime", "com.apple.systempreferences", "com.apple.keychainaccess", "com.apple.Passwords",
    "com.1password.1password", "com.agilebits.onepassword7", "com.bitwarden.desktop",
    "org.keepassxc.keepassxc", "COM.APPLE.SAFARI",
])
def test_never_grantable_applications_are_refused_as_app_and_window_scopes(bundle):
    store, events = _store()
    assert cg.is_grantable_application(bundle, "app") is False
    assert cg.is_grantable_application(bundle, "window") is False
    with pytest.raises(ValueError):
        _create(store, scope={"kind": "app", "bundle_id": bundle})
    with pytest.raises(ValueError):
        _create(store, scope={"kind": "window", "bundle_id": bundle, "hwnd": 7})
    assert events.records == []


def test_finder_is_grantable_only_as_a_reviewed_window():
    store, _ = _store()
    assert cg.is_grantable_application("com.apple.finder", "app") is False
    assert cg.is_grantable_application("com.apple.finder", "window") is True
    with pytest.raises(ValueError):
        _create(store, scope={"kind": "app", "bundle_id": "com.apple.finder"})
    window = _create(store, scope={"kind": "window", "bundle_id": "com.apple.finder", "hwnd": 3})
    assert _match(store, "click", {"bundle_id": "com.apple.finder", "hwnd": 3}) == window
    assert cg.is_grantable_application("com.apple.TextEdit", "app") is True
    assert cg.is_grantable_application("", "app") is False
    assert cg.is_grantable_application(None, "app") is False
    assert cg.is_grantable_application("com.apple.TextEdit", "screen") is False


@pytest.mark.parametrize("argv0", [
    "sh", "bash", "/bin/zsh", "BASH", "fish", "python", "python3", "/usr/bin/python3.12", "node", "ruby",
    "perl", "env", "/usr/bin/env", "xargs", "osascript", "open", "sudo", "su", "launchctl", "defaults",
    "security",
])
def test_never_grantable_programs_are_refused(argv0):
    store, events = _store()
    assert cg.is_grantable_program(argv0) is False
    with pytest.raises(ValueError):
        _create(store, scope={"kind": "command"}, verb_classes=("command",), command={"argv0": argv0, "root": ROOT})
    assert events.records == []


def test_ordinary_programs_stay_grantable_and_invocation_review_is_the_callers_flag():
    for argv0 in ("wc", "git", "find", "ls", "/usr/bin/grep", "jq"):
        assert cg.is_grantable_program(argv0) is True
    assert cg.is_grantable_program("") is False
    assert cg.is_grantable_program(None) is False
    store, _ = _store()
    git = _create(store, scope={"kind": "command"}, verb_classes=("command",), command={"argv0": "git", "root": ROOT})
    assert _match(store, "run_command", {"argv0": "git", "cwd": ROOT}) == git
    assert _match(store, "run_command", {"argv0": "git", "cwd": ROOT, "destructive": True}) is None


def test_destructive_targets_and_uncovered_verbs_are_never_covered():
    store, _ = _store()
    _create(store, scope={"kind": "command"}, verb_classes=("command",), command=CMD)
    assert _match(store, "run_command", {"argv0": "wc", "cwd": ROOT, "destructive": True}) is None
    _create(store, scope=ORIGIN, verb_classes=("browser",))
    assert _match(store, "browser_click", {"origin": "https://example.org", "destructive": True}) is None
    _create(store)
    assert _match(store, "click", {"bundle_id": "com.apple.TextEdit", "destructive": 1}) is None
    for verb in ("write_file", "delete_path", "set_clipboard", "screenshot", "browser_navigate",
                 "browser_click", "browser_fill", "confirm_action", "run_shell"):
        assert _match(store, verb, {"bundle_id": "com.apple.TextEdit", "argv0": "wc", "cwd": ROOT}) is None
    assert _match(store, "click", "com.apple.TextEdit") is None


@pytest.mark.parametrize("field", ["owner_id", "host_id", "chat_id"])
def test_a_grant_is_bound_to_owner_host_and_chat(field):
    store, _ = _store()
    _create(store)
    assert _match(store, "click", {"bundle_id": "com.apple.TextEdit"}, **{field: "other"}) is None


def test_a_grant_survives_later_turns_in_the_same_chat_and_records_its_task():
    store, _ = _store()
    grant = _create(store, task_id="turn-1")
    assert _match(store, "click", {"bundle_id": "com.apple.TextEdit"}) == grant
    assert grant.to_dict()["task_id"] == "turn-1"
    with pytest.raises(TypeError):
        _match(store, "click", {"bundle_id": "com.apple.TextEdit"}, task_id="turn-2")


def test_origin_grant_covers_browser_actions_on_that_origin_only():
    store, _ = _store()
    grant = _create(store, scope={"kind": "origin", "origin": "HTTPS://Example.org:443/"}, verb_classes=("browser",))
    assert grant.scope == ORIGIN
    for verb in sorted(cg.ORIGIN_GRANT_VERBS):
        assert _match(store, verb, {"origin": "https://example.org"}) == grant
    assert _match(store, "browser_navigate", {"origin": "https://EXAMPLE.org:443"}) == grant
    for origin in ("https://example.org:8443", "http://example.org", "https://sub.example.org",
                   "https://example.org/path", "javascript:alert(1)", "", None, 7):
        assert _match(store, "browser_click", {"origin": origin}) is None
    for verb in ("browser_snapshot", "browser_get_text", "browser_screenshot", "browser_wait", "click"):
        assert _match(store, verb, {"origin": "https://example.org", "bundle_id": "com.apple.TextEdit"}) is None
    local = _create(store, scope={"kind": "origin", "origin": "http://[::1]:8080"}, verb_classes=("browser",))
    assert local.scope["origin"] == "http://[::1]:8080"
    assert _match(store, "browser_fill", {"origin": "http://[::1]:8080/"}) == local
    again = _create(store, scope={"kind": "origin", "origin": "https://example.org"}, verb_classes=("browser",))
    assert store.list("s-1") == (local, again)


def test_sliding_expiry_refreshes_on_use_and_never_after_expiry():
    store, events = _store()
    grant = _create(store)
    used = store.touch(grant.grant_id, now=T0 + 800, verb="click", target={"bundle_id": "com.apple.TextEdit", "hwnd": 7})
    assert used.last_used_at == T0 + 800 and used.created_at == T0
    assert events.records[-1][0] == cg.EVENT_USED
    assert events.records[-1][1]["verb"] == "click"
    assert events.records[-1][1]["target"] == {"bundle_id": "com.apple.TextEdit", "hwnd": 7}
    assert _match(store, "click", {"bundle_id": "com.apple.TextEdit"}, now=T0 + 1699) == used
    assert _match(store, "click", {"bundle_id": "com.apple.TextEdit"}, now=T0 + 1700) is None
    assert events.records[-1] == (cg.EVENT_EXPIRED, {**used.to_dict(), "reason": "sliding"})
    assert store.list("s-1") == ()
    assert store.touch(grant.grant_id, now=T0 + 1701, verb="click", target={}) is None


def test_touching_an_expired_grant_removes_it_without_refreshing():
    store, events = _store()
    grant = _create(store)
    assert store.touch(grant.grant_id, now=T0 + cg.SLIDING_TTL_S, verb="click", target={}) is None
    assert events.names() == [cg.EVENT_CREATED, cg.EVENT_EXPIRED]
    assert store.list("s-1") == ()


def test_hard_cap_ends_a_grant_that_is_used_continuously():
    store, events = _store()
    grant = _create(store)
    now = T0
    while now + 800 < T0 + cg.HARD_CAP_S:
        now += 800
        assert store.touch(grant.grant_id, now=now, verb="scroll", target={"bundle_id": "com.apple.TextEdit"})
    assert _match(store, "scroll", {"bundle_id": "com.apple.TextEdit"}, now=T0 + cg.HARD_CAP_S - 1) is not None
    assert _match(store, "scroll", {"bundle_id": "com.apple.TextEdit"}, now=T0 + cg.HARD_CAP_S) is None
    assert events.records[-1][0] == cg.EVENT_EXPIRED and events.records[-1][1]["reason"] == "hard_cap"


def test_expire_sweeps_every_session_and_keeps_live_grants():
    store, events = _store()
    stale = _create(store, session="s-1")
    when = T0 - cg.HARD_CAP_S + 100
    capped = _create(store, session="s-2", now=when)
    while when + 800 < T0 + 100:
        when += 800
        capped = store.touch(capped.grant_id, now=when, verb="click", target={})
    live = _create(store, session="s-3", now=T0 + 500)
    removed = store.expire(now=T0 + cg.SLIDING_TTL_S + 1)
    assert {g.grant_id for g in removed} == {stale.grant_id, capped.grant_id}
    reasons = {p["grant_id"]: p["reason"] for n, p in events.records if n == cg.EVENT_EXPIRED}
    assert reasons == {stale.grant_id: "sliding", capped.grant_id: "hard_cap"}
    assert store.list("s-3") == (live,)
    assert store.expire(now=T0 + cg.SLIDING_TTL_S + 2) == ()


def test_revoke_a_session_or_one_grant_with_a_reason():
    store, events = _store()
    app = _create(store)
    window = _create(store, scope=WINDOW)
    other = _create(store, session="s-2")
    assert store.revoke_grant(window.grant_id, "window_invalid") == window
    assert events.records[-1] == (cg.EVENT_REVOKED, {**window.to_dict(), "reason": "window_invalid"})
    assert store.revoke_grant(window.grant_id, "window_invalid") is None
    assert store.revoke("s-1", "takeover") == (app,)
    assert events.records[-1] == (cg.EVENT_REVOKED, {**app.to_dict(), "reason": "takeover"})
    assert store.revoke("s-1", "stop") == ()
    assert store.list("s-2") == (other,)
    for reason in sorted(cg.REVOCATION_REASONS):
        store.revoke("s-2", reason)
    with pytest.raises(ValueError):
        store.revoke("s-2", "because")
    with pytest.raises(ValueError):
        store.revoke_grant(other.grant_id, "")


def test_a_second_approval_for_the_same_scope_replaces_the_first():
    store, events = _store()
    first = _create(store)
    second = _create(store, scope={"kind": "app", "bundle_id": "com.apple.textedit"}, now=T0 + 10)
    assert first.grant_id != second.grant_id
    assert store.list("s-1") == (second,)
    assert events.names() == [cg.EVENT_CREATED, cg.EVENT_REVOKED, cg.EVENT_CREATED]
    assert events.records[1][1]["reason"] == "replaced"
    wc = _create(store, scope={"kind": "command"}, verb_classes=("command",), command=CMD)
    git = _create(store, scope={"kind": "command"}, verb_classes=("command",), command={"argv0": "git", "root": ROOT})
    wc2 = _create(store, scope={"kind": "command"}, verb_classes=("command",), command={"argv0": "wc", "root": ROOT + "/"})
    assert store.list("s-1") == (second, git, wc2)
    assert wc.grant_id != wc2.grant_id
    window = _create(store, scope=WINDOW)
    assert store.list("s-1") == (second, git, wc2, window)


def test_list_returns_session_grants_in_creation_order():
    store, _ = _store()
    assert store.list("s-1") == ()
    a = _create(store, now=T0)
    b = _create(store, scope={"kind": "app", "bundle_id": "com.apple.Notes"}, now=T0 + 1)
    _create(store, session="s-2")
    assert store.list("s-1") == (a, b)


def test_session_grant_count_is_bounded():
    store, _ = _store()
    for n in range(cg.MAX_GRANTS_PER_SESSION):
        _create(store, scope={"kind": "app", "bundle_id": f"com.example.app{n}"})
    _create(store, scope={"kind": "app", "bundle_id": "com.example.app0"})
    with pytest.raises(ValueError):
        _create(store, scope={"kind": "app", "bundle_id": "com.example.extra"})
    assert len(store.list("s-1")) == cg.MAX_GRANTS_PER_SESSION


@pytest.mark.parametrize("over", [
    {"scope": {"kind": "app"}},
    {"scope": {"kind": "app", "bundle_id": ""}},
    {"scope": {"kind": "app", "bundle_id": "com.apple.TextEdit", "hwnd": 1}},
    {"scope": {"kind": "window", "bundle_id": "com.apple.TextEdit"}},
    {"scope": {"kind": "window", "bundle_id": "com.apple.TextEdit", "hwnd": True}},
    {"scope": {"kind": "window", "bundle_id": "com.apple.TextEdit", "hwnd": 0}},
    {"scope": {"kind": "window", "bundle_id": "com.apple.TextEdit", "hwnd": 2**53 + 1}},
    {"scope": {"kind": "screen"}},
    {"scope": "com.apple.TextEdit"},
    {"scope": {"kind": "command"}},
    {"scope": {"kind": "command", "argv0": "wc"}, "verb_classes": ("command",), "command": CMD},
    {"scope": {"kind": "command"}, "verb_classes": ("input", "command"), "command": CMD},
    {"scope": ORIGIN},
    {"scope": ORIGIN, "verb_classes": ("browser", "input")},
    {"scope": ORIGIN, "verb_classes": ("browser", "command"), "command": CMD},
    {"verb_classes": ("browser",)},
    {"verb_classes": ("input", "browser")},
    {"scope": {"kind": "origin", "origin": "file:///etc"}, "verb_classes": ("browser",)},
    {"scope": {"kind": "origin", "origin": "https://user:pw@example.org"}, "verb_classes": ("browser",)},
    {"scope": {"kind": "origin", "origin": "https://example.org/path"}, "verb_classes": ("browser",)},
    {"scope": {"kind": "origin", "origin": "https://example.org/?q=1"}, "verb_classes": ("browser",)},
    {"scope": {"kind": "origin", "origin": "https://example.org/#f"}, "verb_classes": ("browser",)},
    {"scope": {"kind": "origin", "origin": "example.org"}, "verb_classes": ("browser",)},
    {"scope": {"kind": "origin", "origin": "https://"}, "verb_classes": ("browser",)},
    {"scope": {"kind": "origin", "origin": "https://example.org:99999"}, "verb_classes": ("browser",)},
    {"scope": {"kind": "origin", "origin": "https://exa mple.org"}, "verb_classes": ("browser",)},
    {"scope": {"kind": "origin", "origin": 7}, "verb_classes": ("browser",)},
    {"scope": {"kind": "origin", "origin": "https://example.org", "extra": 1}, "verb_classes": ("browser",)},
    {"verb_classes": ()},
    {"verb_classes": ("observe",)},
    {"verb_classes": "input"},
    {"verb_classes": ("command",), "command": CMD},
    {"verb_classes": ("input", "command")},
    {"command": CMD},
    {"verb_classes": ("input", "command"), "command": {"argv0": "wc"}},
    {"verb_classes": ("input", "command"), "command": {"argv0": "", "root": ROOT}},
    {"verb_classes": ("input", "command"), "command": {"argv0": "w c", "root": ROOT}},
    {"verb_classes": ("input", "command"), "command": {"argv0": "wc", "root": "relative"}},
    {"verb_classes": ("input", "command"), "command": {"argv0": "wc", "root": "/"}},
    {"verb_classes": ("input", "command"), "command": {"argv0": "wc", "root": "/a/../.."}},
    {"verb_classes": ("input", "command"), "command": {"argv0": "wc", "root": ROOT, "extra": 1}},
    {"owner_id": ""},
    {"task_id": "t\x00"},
    {"chat_id": 7},
    {"session": "x" * 300},
    {"now": True},
    {"now": float("nan")},
])
def test_create_refuses_malformed_grants(over):
    store, events = _store()
    with pytest.raises(ValueError):
        _create(store, **over)
    assert events.records == []


def test_grant_serializes_to_the_contract_shape():
    store, _ = _store()
    grant = _create(store, verb_classes=("command", "input"), command={"argv0": "wc", "root": ROOT + "/"})
    data = grant.to_dict()
    assert set(data) == {"grant_id", "owner_id", "host_id", "session_id", "chat_id", "task_id", "scope",
                         "verb_classes", "command", "created_at", "last_used_at", "sliding_ttl_s", "hard_cap_s"}
    assert data["verb_classes"] == ["command", "input"]
    assert data["command"] == {"argv0": "wc", "root": ROOT}
    assert data["scope"] == APP
    assert data["created_at"] == data["last_used_at"] == "2027-01-15T08:00:00Z"
    assert (data["sliding_ttl_s"], data["hard_cap_s"]) == (900, 7200)
    assert grant.expires_at == T0 + 900
    assert _create(store, scope=WINDOW).to_dict()["command"] is None
    with pytest.raises(TypeError):
        grant.scope["bundle_id"] = "x"


def test_the_events_hook_is_optional():
    store = GrantStore()
    grant = store.create("o", "h", "s", "c", "t", APP, ("input",), now=T0)
    assert store.match("s", "click", {"bundle_id": "com.apple.TextEdit"}, now=T0 + 1,
                       owner_id="o", host_id="h", chat_id="c") == grant
    assert store.touch(grant.grant_id, now=T0 + 2, verb="click", target={}) is not None
    assert store.revoke("s", "pause") != ()
    assert store.expire(now=T0) == ()


def test_event_names_are_the_audit_vocabulary():
    assert (cg.EVENT_CREATED, cg.EVENT_USED, cg.EVENT_REVOKED, cg.EVENT_EXPIRED) == (
        "computer_grant.created", "computer_grant.used", "computer_grant.revoked", "computer_grant.expired")
    assert (cg.SLIDING_TTL_S, cg.HARD_CAP_S) == (900, 7200)
    assert {"pause", "stop", "takeover", "socket_lost", "host_replaced", "authority_changed",
            "chat_changed", "app_changed", "window_invalid", "replaced"} <= cg.REVOCATION_REASONS
    assert "task_ended" not in cg.REVOCATION_REASONS


def test_used_records_carry_only_a_bounded_target_summary():
    store, events = _store()
    grant = _create(store)
    store.touch(grant.grant_id, now=T0 + 1, verb="run_command", target={"argv0": "wc", "cwd": "/" + "a" * 400,
                                                                         "hwnd": 0, "secret": "x"})
    assert events.records[-1][1]["target"] == {"argv0": "wc", "cwd": "/" + "a" * 199}
    store.touch(grant.grant_id, now=T0 + 2, verb="click", target=None)
    assert events.records[-1][1]["target"] == {}
