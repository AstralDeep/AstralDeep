"""Holds task-scoped computer-use grants in orchestrator memory: creation from an approval
decision, matching covered verbs by application, window, command folder or browser origin,
sliding and hard expiry, and revocation; verb classes come from computer_use_policy, and audit
records leave through the events callable the orchestrator injects.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import math
import posixpath
import threading
from types import MappingProxyType
from typing import Any, Callable, Dict, FrozenSet, Mapping, Optional, Tuple
from urllib.parse import urlsplit
import uuid

from orchestrator.computer_use_policy import COMMAND_VERBS, is_shell_app

EVENT_CREATED = "computer_grant.created"
EVENT_USED = "computer_grant.used"
EVENT_REVOKED = "computer_grant.revoked"
EVENT_EXPIRED = "computer_grant.expired"

SLIDING_TTL_S = 900
HARD_CAP_S = 7200
MAX_GRANTS_PER_SESSION = 32

INPUT_GRANT_VERBS: FrozenSet[str] = frozenset({
    "click", "double_click", "right_click", "move", "drag", "scroll", "type_text",
    "press_keys", "focus_window", "open_app",
})
ORIGIN_GRANT_VERBS: FrozenSet[str] = frozenset({
    "browser_navigate", "browser_click", "browser_fill", "browser_press", "browser_scroll", "browser_dialog",
})
VERB_CLASSES: FrozenSet[str] = frozenset({"input", "command", "browser"})
REVOCATION_REASONS: FrozenSet[str] = frozenset({
    "pause", "stop", "takeover", "socket_lost", "host_replaced", "authority_changed",
    "chat_changed", "app_changed", "window_invalid", "session_ended", "replaced", "user_revoked",
})

_NEVER_GRANTABLE_BUNDLES: FrozenSet[str] = frozenset({
    "com.apple.safari", "com.apple.safaritechnologypreview", "org.mozilla.firefox",
    "org.mozilla.firefoxdeveloperedition", "org.mozilla.nightly", "company.thebrowser.browser",
    "org.chromium.chromium", "com.kagi.kagimacos", "org.torproject.torbrowser", "com.duckduckgo.macos.browser",
    "com.apple.mail", "com.apple.mobilesms", "com.apple.facetime", "com.apple.systempreferences",
    "com.apple.keychainaccess", "com.apple.passwords", "com.bitwarden.desktop", "org.keepassxc.keepassxc",
    "com.lastpass.lastpass", "in.sinew.enpass-desktop", "com.dashlane.dashlanephonefinal",
    "me.proton.pass.electron", "com.microsoft.vscode", "com.microsoft.vscodeinsiders",
    "com.todesktop.230313mzl4w4u92",
})
_NEVER_GRANTABLE_BUNDLE_PREFIXES: Tuple[str, ...] = (
    "com.google.chrome", "com.microsoft.edge", "com.brave.browser", "com.operasoftware.", "com.vivaldi.",
    "com.1password.", "com.agilebits.onepassword", "com.jetbrains.",
)
_NEVER_GRANTABLE_PROGRAMS: FrozenSet[str] = frozenset({
    "sh", "bash", "zsh", "fish", "node", "ruby", "perl", "env", "xargs", "osascript", "open", "sudo",
    "su", "launchctl", "defaults", "security", "nice", "nohup", "time", "command", "exec", "caffeinate",
    "awk", "gawk", "sed", "make", "xcrun", "swift", "php", "lua", "tclsh", "script", "expect", "screen",
    "tmux",
})

_ID_MAX = 256
_ARGV0_MAX = 255
_PATH_MAX = 1024
_TARGET_TEXT_MAX = 200
_HWND_MAX = 2**53

Events = Callable[[str, Dict[str, Any]], None]


def _rfc3339(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value: Any, name: str, limit: int = _ID_MAX) -> str:
    if (not isinstance(value, str) or not value or len(value) > limit
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        raise ValueError(f"{name} must be 1-{limit} printable characters")
    return value


def _instant(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("now must be a finite number of seconds")
    return float(value)


def _hwnd(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _HWND_MAX:
        return None
    return value


def _absolute(value: Any) -> Optional[str]:
    if (not isinstance(value, str) or not value.startswith("/") or len(value) > _PATH_MAX
            or "\x00" in value):
        return None
    return "/" + posixpath.normpath(value).lstrip("/")


def is_grantable_application(bundle_id: Any, kind: str) -> bool:
    if not isinstance(bundle_id, str) or not bundle_id or kind not in {"app", "window"}:
        return False
    folded = bundle_id.casefold()
    if (is_shell_app(bundle_id) or "terminal" in folded or "cursor" in folded
            or folded in _NEVER_GRANTABLE_BUNDLES or folded.startswith(_NEVER_GRANTABLE_BUNDLE_PREFIXES)):
        return False
    return not (kind == "app" and folded == "com.apple.finder")


def is_grantable_program(argv0: Any) -> bool:
    if not isinstance(argv0, str) or not argv0:
        return False
    name = posixpath.basename(argv0).casefold()
    return bool(name) and name not in _NEVER_GRANTABLE_PROGRAMS and not name.startswith("python")


def _origin(value: Any) -> Optional[str]:
    if (not isinstance(value, str) or not value or len(value) > _PATH_MAX
            or any(ord(ch) <= 32 or ord(ch) == 127 for ch in value)):
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if (scheme not in {"http", "https"} or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.path not in {"", "/"} or parts.query or parts.fragment
            or value.rstrip("/").endswith(("?", "#"))):
        return None
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    default = 80 if scheme == "http" else 443
    return f"{scheme}://{host}" + ("" if port in (None, default) else f":{port}")


def _scope(scope: Any, classes: FrozenSet[str], command: Optional[Mapping[str, str]]) -> Dict[str, Any]:
    if not isinstance(scope, Mapping):
        raise ValueError("scope must be an object")
    kind = scope.get("kind")
    if kind in {"app", "window"}:
        if "input" not in classes or "browser" in classes:
            raise ValueError("application and window grants carry the input class and never the browser class")
        bundle = _text(scope.get("bundle_id"), "scope.bundle_id")
        if not is_grantable_application(bundle, kind):
            raise ValueError("this application stays on exact review and cannot carry a task grant")
    if kind == "app":
        if set(scope) != {"kind", "bundle_id"}:
            raise ValueError("app scope must contain exactly kind and bundle_id")
        return {"kind": "app", "bundle_id": bundle}
    if kind == "window":
        if set(scope) != {"kind", "bundle_id", "hwnd"} or _hwnd(scope["hwnd"]) is None:
            raise ValueError("window scope must contain kind, bundle_id and a positive integer hwnd")
        return {"kind": "window", "bundle_id": bundle, "hwnd": scope["hwnd"]}
    if kind == "command":
        if set(scope) != {"kind"} or classes != {"command"} or command is None:
            raise ValueError("command scope carries only kind and needs exactly the command class")
        return {"kind": "command"}
    if kind == "origin":
        origin = _origin(scope.get("origin"))
        if set(scope) != {"kind", "origin"} or classes != {"browser"} or origin is None:
            raise ValueError("origin scope needs one http or https origin and exactly the browser class")
        return {"kind": "origin", "origin": origin}
    raise ValueError("scope.kind must be app, window, command or origin")


def _command(command: Any) -> Optional[Dict[str, str]]:
    if command is None:
        return None
    if not isinstance(command, Mapping) or set(command) != {"argv0", "root"}:
        raise ValueError("command must contain exactly argv0 and root")
    argv0 = _text(command["argv0"], "command.argv0", _ARGV0_MAX)
    if any(ch.isspace() for ch in argv0):
        raise ValueError("command.argv0 must be one program name without whitespace")
    if not is_grantable_program(argv0):
        raise ValueError("this program stays on exact review and cannot carry a task grant")
    root = _absolute(command["root"])
    if root is None or root == "/":
        raise ValueError("command.root must be an absolute folder other than /")
    return {"argv0": argv0, "root": root}


def _classes(verb_classes: Any) -> FrozenSet[str]:
    if isinstance(verb_classes, (str, bytes)) or not isinstance(verb_classes, (tuple, list, set, frozenset)):
        raise ValueError("verb_classes must be a collection of class names")
    classes = frozenset(verb_classes)
    if not classes or not classes <= VERB_CLASSES:
        raise ValueError("verb_classes must name input and/or command")
    return classes


def _key(scope: Mapping[str, Any], command: Optional[Mapping[str, str]]) -> Tuple[Any, ...]:
    if scope["kind"] == "command":
        return ("command", command["argv0"], command["root"])
    if scope["kind"] == "origin":
        return ("origin", scope["origin"])
    if scope["kind"] == "window":
        return ("window", scope["bundle_id"].casefold(), scope["hwnd"])
    return ("app", scope["bundle_id"].casefold())


def _target_summary(target: Any) -> Dict[str, Any]:
    if not isinstance(target, Mapping):
        return {}
    out: Dict[str, Any] = {}
    for name in ("bundle_id", "argv0", "cwd", "origin"):
        value = target.get(name)
        if isinstance(value, str):
            out[name] = value[:_TARGET_TEXT_MAX]
    hwnd = _hwnd(target.get("hwnd"))
    if hwnd is not None:
        out["hwnd"] = hwnd
    return out


@dataclass(frozen=True)
class TaskGrant:
    grant_id: str
    owner_id: str
    host_id: str
    session_id: str
    chat_id: str
    task_id: str
    scope: Mapping[str, Any] = field(hash=False)
    verb_classes: FrozenSet[str]
    command: Optional[Mapping[str, str]] = field(hash=False)
    created_at: float
    last_used_at: float
    sliding_ttl_s: int = SLIDING_TTL_S
    hard_cap_s: int = HARD_CAP_S

    @property
    def expires_at(self) -> float:
        return min(self.last_used_at + self.sliding_ttl_s, self.created_at + self.hard_cap_s)

    def expired_reason(self, now: float) -> Optional[str]:
        if now >= self.created_at + self.hard_cap_s:
            return "hard_cap"
        if now >= self.last_used_at + self.sliding_ttl_s:
            return "sliding"
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "owner_id": self.owner_id,
            "host_id": self.host_id,
            "session_id": self.session_id,
            "chat_id": self.chat_id,
            "task_id": self.task_id,
            "scope": dict(self.scope),
            "verb_classes": sorted(self.verb_classes),
            "command": dict(self.command) if self.command is not None else None,
            "created_at": _rfc3339(self.created_at),
            "last_used_at": _rfc3339(self.last_used_at),
            "sliding_ttl_s": self.sliding_ttl_s,
            "hard_cap_s": self.hard_cap_s,
        }

    def _bound(self, owner_id: str, host_id: str, chat_id: str) -> bool:
        return (self.owner_id, self.host_id, self.chat_id) == (owner_id, host_id, chat_id)

    def _covers(self, verb: str, target: Mapping[str, Any]) -> bool:
        if target.get("destructive"):
            return False
        if verb in ORIGIN_GRANT_VERBS:
            return self.scope["kind"] == "origin" and _origin(target.get("origin")) == self.scope["origin"]
        if verb in INPUT_GRANT_VERBS:
            if "input" not in self.verb_classes or self.scope["kind"] not in {"app", "window"}:
                return False
            bundle = target.get("bundle_id")
            if not isinstance(bundle, str) or bundle.casefold() != self.scope["bundle_id"].casefold():
                return False
            if "hwnd" in target and _hwnd(target["hwnd"]) is None:
                return False
            return self.scope["kind"] == "app" or target.get("hwnd") == self.scope["hwnd"]
        if verb in COMMAND_VERBS:
            if "command" not in self.verb_classes or self.command is None:
                return False
            cwd = _absolute(target.get("cwd"))
            root = self.command["root"]
            return (target.get("argv0") == self.command["argv0"] and cwd is not None
                    and (cwd == root or cwd.startswith(root + "/")))
        return False


class GrantStore:
    def __init__(self, events: Optional[Events] = None):
        self._events = events
        self._lock = threading.Lock()
        self._by_session: Dict[str, Dict[Tuple[Any, ...], TaskGrant]] = {}
        self._index: Dict[str, Tuple[str, Tuple[Any, ...]]] = {}

    def create(self, owner_id: str, host_id: str, session_id: str, chat_id: str, task_id: str,
               scope: Mapping[str, Any], verb_classes: Any, command: Optional[Mapping[str, str]] = None,
               *, now: float) -> TaskGrant:
        ids = [_text(value, name) for value, name in ((owner_id, "owner_id"), (host_id, "host_id"),
               (session_id, "session_id"), (chat_id, "chat_id"), (task_id, "task_id"))]
        at = _instant(now)
        classes = _classes(verb_classes)
        normalized_command = _command(command)
        if ("command" in classes) != (normalized_command is not None):
            raise ValueError("command is required exactly when verb_classes includes command")
        normalized_scope = _scope(scope, classes, normalized_command)
        key = _key(normalized_scope, normalized_command)
        grant = TaskGrant(
            str(uuid.uuid4()), *ids,
            scope=MappingProxyType(normalized_scope),
            verb_classes=classes,
            command=MappingProxyType(normalized_command) if normalized_command is not None else None,
            created_at=at, last_used_at=at,
        )
        with self._lock:
            grants = self._by_session.setdefault(grant.session_id, {})
            replaced = grants.pop(key, None)
            if replaced is None and len(grants) >= MAX_GRANTS_PER_SESSION:
                raise ValueError("too many task grants for this session")
            if replaced is not None:
                self._index.pop(replaced.grant_id, None)
            grants[key] = grant
            self._index[grant.grant_id] = (grant.session_id, key)
        if replaced is not None:
            self._emit(EVENT_REVOKED, replaced, reason="replaced")
        self._emit(EVENT_CREATED, grant)
        return grant

    def match(self, session_id: str, verb: str, target: Any, *, now: float, owner_id: str,
              host_id: str, chat_id: str) -> Optional[TaskGrant]:
        at = _instant(now)
        if not isinstance(target, Mapping):
            return None
        expired = []
        found = None
        with self._lock:
            grants = self._by_session.get(session_id, {})
            for key, grant in sorted(grants.items(), key=lambda item: item[0][0] != "window"):
                reason = grant.expired_reason(at)
                if reason is not None:
                    expired.append((grant, reason))
                    continue
                if found is None and grant._bound(owner_id, host_id, chat_id) and grant._covers(verb, target):
                    found = grant
            for grant, _ in expired:
                self._remove(grant)
        for grant, reason in expired:
            self._emit(EVENT_EXPIRED, grant, reason=reason)
        return found

    def touch(self, grant_id: str, *, now: float, verb: str, target: Any) -> Optional[TaskGrant]:
        at = _instant(now)
        with self._lock:
            grant = self._get(grant_id)
            if grant is None:
                return None
            reason = grant.expired_reason(at)
            if reason is None:
                grant = replace(grant, last_used_at=max(grant.last_used_at, at))
                session_id, key = self._index[grant_id]
                self._by_session[session_id][key] = grant
            else:
                self._remove(grant)
        if reason is not None:
            self._emit(EVENT_EXPIRED, grant, reason=reason)
            return None
        self._emit(EVENT_USED, grant, verb=str(verb)[:64], target=_target_summary(target))
        return grant

    def revoke(self, session_id: str, reason: str) -> Tuple[TaskGrant, ...]:
        self._reason(reason)
        with self._lock:
            removed = tuple(self._by_session.pop(session_id, {}).values())
            for grant in removed:
                self._index.pop(grant.grant_id, None)
        for grant in removed:
            self._emit(EVENT_REVOKED, grant, reason=reason)
        return removed

    def revoke_grant(self, grant_id: str, reason: str) -> Optional[TaskGrant]:
        self._reason(reason)
        with self._lock:
            grant = self._get(grant_id)
            if grant is not None:
                self._remove(grant)
        if grant is not None:
            self._emit(EVENT_REVOKED, grant, reason=reason)
        return grant

    def expire(self, *, now: float) -> Tuple[TaskGrant, ...]:
        at = _instant(now)
        with self._lock:
            expired = [(grant, grant.expired_reason(at)) for grants in self._by_session.values()
                       for grant in grants.values()]
            expired = [(grant, reason) for grant, reason in expired if reason is not None]
            for grant, _ in expired:
                self._remove(grant)
        for grant, reason in expired:
            self._emit(EVENT_EXPIRED, grant, reason=reason)
        return tuple(grant for grant, _ in expired)

    def list(self, session_id: str) -> Tuple[TaskGrant, ...]:
        with self._lock:
            return tuple(self._by_session.get(session_id, {}).values())

    def _get(self, grant_id: str) -> Optional[TaskGrant]:
        location = self._index.get(grant_id)
        if location is None:
            return None
        return self._by_session[location[0]][location[1]]

    def _remove(self, grant: TaskGrant) -> None:
        session_id, key = self._index.pop(grant.grant_id)
        grants = self._by_session[session_id]
        del grants[key]
        if not grants:
            del self._by_session[session_id]

    @staticmethod
    def _reason(reason: Any) -> None:
        if reason not in REVOCATION_REASONS:
            raise ValueError(f"revocation reason must be one of {sorted(REVOCATION_REASONS)}")

    def _emit(self, name: str, grant: TaskGrant, **extra: Any) -> None:
        if self._events is not None:
            self._events(name, {**grant.to_dict(), **extra})
