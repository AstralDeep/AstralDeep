"""BYO agent host and v2 runtime-fencing coordinator (058/060).

This is the desktop half of `specs/058-byo-agents-runtime/contracts/host-bundle.md`.
The orchestrator generates a self-contained 3-file bundle and pushes it down the
owner's authenticated UI socket; this module writes it to disk, runs it as a
**separate child process**, and pumps frames between that child and the socket:

    orchestrator ──ws(agent_tunnel)──► client ──stdin (json lines)──► child
                 ◄─ws(agent_tunnel)───        ◄─stdout (json lines)──

Feature 060 adds a server-issued session/delivery/revision/runtime fence. The
host validates that structural fence and bounded child protocol locally before
forwarding it; the orchestrator remains the authorization boundary and repeats
all owner, permission, delegation, PHI, generation, and selection checks.

Why a child process and not a thread (unlike `win_agent/agent.py`, the built-in
Windows tools agent, which is an in-process aiohttp server the orchestrator
dials INTO): the code is LLM-written and user-owned. It gets its own process so
a crash, a `sys.exit()`, a runaway loop or a blocking C call cannot take the GUI
with it, and so termination is a real kill rather than a cooperative request.

No Qt in this module — the child pump runs on plain threads, so it is testable
without a QApplication and cannot touch a widget from the wrong thread. Callers
pass a `notify` callable that marshals to the GUI thread (a Qt signal's `.emit`).
"""
from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import shutil
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from win_agent.process_supervision import (
    OutputStream,
    ProcessSupervisor,
    TerminationReason,
)

logger = logging.getLogger("astral.client.byo")

#: The frame types this host owns on the inbound UI socket.
HOST_FRAME_TYPES = (
    "agent_host_registered",
    "agent_host_registration_refused",
    "agent_host_inventory_reconciled",
    "agent_bundle_deliver",
    "agent_tunnel",
    "agent_stop",
    "agent_offline",
)

BYO_RUNTIME_CONTRACT_VERSION = 2
PACKAGED_RUNTIME_LOCK_ARTIFACT = "requirements-release.lock.txt"
PACKAGED_RUNTIME_LOCK_SHA256 = (
    "82d58a54a8cd1a7ffd925d724ba247f0cfb09e4de0747aeca4a869f7dd87ba35"
)
BUNDLE_FILE_NAMES = ("agent_main.py", "astralprims_ui.py", "mcp_tools.py")
_RUNTIME_METADATA_FILE = ".astraldeep-runtime.json"
_HOST_IDENTITY_FILE = ".host-identity.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_REASON = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_CHILD_ENV_ALLOWLIST = {
    "APPDATA",
    "COMSPEC",
    "HOME",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
}

#: How long to wait for the server's `agent_registered` ack after starting a
#: child. THE SILENCE TRAP (contract §6): a REFUSED registration produces no
#: frame at all — the orchestrator closes a `TunnelSocket`, whose `close()` is a
#: parity no-op, and there is no NAK in the protocol. Waiting forever on a frame
#: that will never come would leave a zombie child and a permanently "starting"
#: agent, so silence is treated as failure.
REGISTER_TIMEOUT_S = float(os.getenv("BYO_REGISTER_TIMEOUT_S", "20"))
HOST_ACK_TIMEOUT_S = 2.0

#: An agent_id is used as a DIRECTORY NAME under the agents root, so the charset
#: alone is not enough: `.` and `..` match it and would escape (or clobber) the
#: root. Anything starting with a dot is refused, and the resolved path is
#: re-checked against the root before a single byte is written (`_agent_dir`).
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$")

#: A revision reuses the SAME agent_id, so its new bundle is staged under
#: `<agent_id>.pending` beside the live one: the revised child runs from there
#: and the directories are swapped only once it has registered inward (T027
#: host-side rollover). A failed revision therefore never touches the version the
#: owner is relying on. rehydrate() skips these — a staging dir is not an agent id,
#: and a crash mid-revision must not resurrect a half-written one on next launch.
_PENDING_SUFFIX = ".pending"


class HostIdentityError(RuntimeError):
    """The persisted installation identity is unreadable or malformed."""


def _canonical_uuid4(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a UUID4 string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{name} must be a UUID4 string") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{name} must be a canonical UUID4 string")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _validate_utc(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{name} must be RFC3339 UTC")
    datetime.fromisoformat(f"{value[:-1]}+00:00")
    return value


def _fsync_directory(directory: str) -> None:
    """Flush directory metadata where the host OS exposes a directory handle."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        # Windows may refuse directory opens through os.open. Atomic replacement
        # is still used, and each file itself has already been flushed.
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_install_rename(staging: str, destination: str) -> None:
    """Create-only atomic revision rename, write-through on Windows."""

    if os.name == "nt":  # pragma: no cover - exercised by Windows CI
        import ctypes

        movefile_write_through = 0x00000008
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.MoveFileExW.argtypes = (
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint,
        )
        kernel32.MoveFileExW.restype = ctypes.c_int
        if not kernel32.MoveFileExW(staging, destination, movefile_write_through):
            raise ctypes.WinError(ctypes.get_last_error())
        return
    # A non-empty immutable destination cannot be replaced by a directory
    # rename on POSIX, so this remains create-only under a racing duplicate.
    os.rename(staging, destination)


def load_or_create_host_id(base_dir: Optional[str] = None) -> str:
    """Return one UUID4 persisted for this desktop installation.

    Creation uses a same-directory temporary plus a create-only hard link so
    concurrent application starts cannot each leave with a different identity.
    A malformed existing identity fails closed instead of silently changing the
    machine's selection identity.
    """

    root = os.path.realpath(base_dir or agents_root())
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, _HOST_IDENTITY_FILE)

    def read_existing() -> str:
        try:
            with open(path, encoding="utf-8") as handle:
                value = json.load(handle)
            if set(value) != {"schema_version", "host_id"} or value["schema_version"] != 1:
                raise ValueError("identity fields are invalid")
            return _canonical_uuid4(value["host_id"], "host_id")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise HostIdentityError("persisted BYO host identity is invalid") from exc

    if os.path.exists(path):
        return read_existing()

    candidate = str(uuid.uuid4())
    payload = json.dumps(
        {"schema_version": 1, "host_id": candidate},
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    temporary = os.path.join(root, f".{_HOST_IDENTITY_FILE}.{uuid.uuid4()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        except OSError:
            # Same-filesystem hard links are available on supported Windows and
            # POSIX release targets. This create-only fallback retains safety on
            # restricted filesystems without replacing an existing identity.
            try:
                target = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(target, "w", encoding="utf-8", newline="") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
        _fsync_directory(root)
    finally:
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass
    return read_existing()


def canonical_bundle_sha256(files: dict[str, str]) -> str:
    """Digest the complete v2 three-file mapping as canonical UTF-8 JSON."""

    if set(files) != set(BUNDLE_FILE_NAMES) or any(
        not isinstance(name, str) or not isinstance(source, str)
        for name, source in files.items()
    ):
        raise ValueError("v2 bundles contain exactly the three declared text files")
    canonical = json.dumps(
        files,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def check_runtime_compatibility(
    runtime_contract_version: object, required_runtime_lock_sha256: object
) -> Optional[str]:
    """Return the canonical refusal code, or None for the packaged runtime."""

    if runtime_contract_version != BYO_RUNTIME_CONTRACT_VERSION:
        return "runtime_contract_unsupported"
    if required_runtime_lock_sha256 != PACKAGED_RUNTIME_LOCK_SHA256:
        return "runtime_lock_mismatch"
    return None


def _child_environment() -> dict[str, str]:
    """Minimal OS environment; never inherit credentials into authored code."""

    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _CHILD_ENV_ALLOWLIST
    }


def agents_root() -> str:
    """`%LOCALAPPDATA%/AstralDeep/agents` (contract §5); `~/.astraldeep/agents`
    where LOCALAPPDATA is absent, so the module imports and tests on any OS."""
    base = os.getenv("BYO_AGENTS_DIR") or os.getenv("LOCALAPPDATA")
    if base:
        return os.path.join(base, "AstralDeep", "agents")
    return os.path.join(os.path.expanduser("~"), ".astraldeep", "agents")


def worker_argv(agent_dir: str) -> List[str]:
    """The command that re-invokes THIS application as a worker (contract §4).

    Frozen (PyInstaller onefile, `console=False`), `sys.executable` IS
    AstralDeep.exe, so the flag goes straight to it — and `main.py` must branch
    on it before Qt loads or every worker would raise a second GUI. Run from
    source, `sys.executable` is python.exe, which would treat `--byo-worker` as
    its own (unknown) option, so the script path has to be passed explicitly.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--byo-worker", agent_dir]
    main_py = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"
    )
    return [sys.executable, main_py, "--byo-worker", agent_dir]


class _Child:
    """One supervised user agent."""

    def __init__(self, agent_id: str, proc, directory: str, supervised=None) -> None:
        self.agent_id = agent_id
        self.proc = proc
        self.supervised = supervised
        self.dir = directory
        self.registered = False
        #: Last `register_agent` the child emitted — replayed on socket
        #: reconnect, because the server pops `self.agents[agent_id]` on teardown
        #: and would otherwise never route to this (still running) child again.
        self.register_frame: Optional[str] = None
        self.timer: Optional[threading.Timer] = None
        self.threads: List[threading.Thread] = []

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


@dataclass(frozen=True)
class _InstalledRevision:
    agent_id: str
    revision_id: str
    directory: str
    bundle_sha256: str
    runtime_contract_version: int
    required_runtime_lock_sha256: str


class _RuntimeChild:
    def __init__(
        self,
        *,
        installed: _InstalledRevision,
        fence: dict[str, Any],
        supervised,
    ) -> None:
        self.installed = installed
        self.agent_id = installed.agent_id
        self.fence = dict(fence)
        self.supervised = supervised
        self.proc = supervised.raw_process
        self.registered = False
        self.ready = False
        self.protocol_frame_seen = False
        self.register_timer: Optional[threading.Timer] = None
        self.heartbeat_timer: Optional[threading.Timer] = None
        self.last_heartbeat_sequence = 0
        self.requested_exit_kind: Optional[str] = None
        self.suppress_exit = False
        self.exit_sent = False
        self.exit_lock = threading.Lock()

    def alive(self) -> bool:
        return self.supervised.poll() is None


class ByoAgentHost:
    """Supervises the user's BYO agents for one client session."""

    def __init__(
        self,
        send_event: Optional[Callable[[str, dict], None]] = None,
        notify: Optional[Callable[[str, str], None]] = None,
        base_dir: Optional[str] = None,
        spawn: Optional[Callable[[List[str]], object]] = None,
        register_timeout: float = REGISTER_TIMEOUT_S,
        *,
        send_frame: Optional[Callable[[dict[str, Any]], None]] = None,
        host_id: Optional[str] = None,
        process_supervisor: Optional[ProcessSupervisor] = None,
        process_id_factory: Callable[[], uuid.UUID] = uuid.uuid4,
        heartbeat_interval: float = 1.0,
        host_ack_timeout: float = HOST_ACK_TIMEOUT_S,
        deployment_profile_digest: Optional[str] = None,
    ) -> None:
        if send_event is None and send_frame is None:
            raise TypeError("send_event or send_frame is required")
        self._send_event = send_event or (
            lambda action, payload: send_frame({"type": action, **payload})  # type: ignore[misc]
        )
        self._send_frame = send_frame or (
            lambda frame: self._send_event(
                str(frame["type"]),
                {key: value for key, value in frame.items() if key != "type"},
            )
        )
        self._notify = notify or (lambda text, level="info": None)
        self._base_dir = base_dir or agents_root()
        self._spawn = spawn
        self._register_timeout = register_timeout
        self._process_supervisor = process_supervisor or ProcessSupervisor()
        self._process_id_factory = process_id_factory
        if heartbeat_interval <= 0 or heartbeat_interval > 1.0:
            raise ValueError("heartbeat interval must be positive and at most one second")
        self._heartbeat_interval = heartbeat_interval
        if host_ack_timeout <= 0 or host_ack_timeout > HOST_ACK_TIMEOUT_S:
            raise ValueError("host acknowledgement timeout must be within two seconds")
        self._host_ack_timeout = host_ack_timeout
        if (
            deployment_profile_digest is not None
            and _SHA256.fullmatch(deployment_profile_digest) is None
        ):
            raise ValueError("deployment profile digest must be lowercase SHA-256")
        self.deployment_profile_digest = deployment_profile_digest
        self._host_ack_timer: Optional[threading.Timer] = None
        self.host_id = _canonical_uuid4(
            host_id or load_or_create_host_id(self._base_dir), "host_id"
        )
        # Identifies this host process to the server for the life of the client
        # (stamped on `user_agent.host_session_id` at registration).
        self.host_session_id = uuid.uuid4().hex
        self._accepted_host_session_id: Optional[str] = None
        self._inventory_id: Optional[str] = None
        self._inventory_entries: dict[tuple[str, str], _InstalledRevision] = {}
        self._inventory_completed = False
        self._children: Dict[str, _Child] = {}
        #: In-flight revisions, keyed by the same agent_id as the live child they
        #: will replace. A pending child runs from a `.pending` staging dir and is
        #: promoted into `_children` (retiring the old one) only on its ack.
        self._pending: Dict[str, _Child] = {}
        self._runtime_children: Dict[str, _RuntimeChild] = {}
        self._launching_runtime_instances: set[str] = set()
        self._lock = threading.Lock()
        self._rehydrated = False

    def _agent_dir(self, agent_id: str) -> Optional[str]:
        """The on-disk directory for one agent, or None if the id cannot own one.

        Defence in depth: the delivering server is trusted and namespaces the id,
        but this path is the one place a bad id turns into a WRITE — so the id is
        charset+dot checked AND the resolved target is asserted to sit under the
        agents root before anything is created.
        """
        if not _SAFE_ID.match(agent_id or ""):
            return None
        base = os.path.realpath(self._base_dir)
        target = os.path.realpath(os.path.join(self._base_dir, agent_id))
        try:
            if target == base or os.path.commonpath([base, target]) != base:
                return None
        except ValueError:  # different drives on Windows: not under the root
            return None
        return os.path.join(self._base_dir, agent_id)

    # --- inbound (server -> host) ----------------------------------------- #

    def handle_frame(self, msg: dict) -> bool:
        """Route one server frame. Returns True if this host consumed it."""
        t = msg.get("type")
        if t == "agent_host_registered":
            self.on_host_registered(msg)
        elif t == "agent_host_registration_refused":
            self.on_host_registration_refused(msg)
        elif t == "agent_host_inventory_reconciled":
            self._reconcile_inventory(msg)
        elif t == "agent_bundle_deliver":
            if "fence" in msg or "runtime_contract_version" in msg:
                self._deliver_v2(msg)
            elif self._spawn is not None:
                # Test-only/feature-058 compatibility path. Production hosting
                # advertises only v2 and never silently treats v1 as v2.
                self.deliver(
                    msg.get("agent_id") or "",
                    msg.get("files") or {},
                    msg.get("constitution_version"),
                )
            else:
                logger.warning("refusing an implicit legacy BYO bundle on the v2 host")
        elif t == "agent_tunnel":
            if isinstance(msg.get("fence"), dict):
                self._to_runtime_child(msg)
            else:
                self._to_child(msg.get("agent_id") or "", msg.get("frame"))
        elif t == "agent_stop":
            if isinstance(msg.get("fence"), dict):
                self._stop_runtime_fence(msg["fence"])
            else:
                # A legacy server stop is terminal and removes its mutable v1
                # bundle. V2 immutable revision deletion arrives via inventory.
                self.remove(msg.get("agent_id") or "")
        elif t == "agent_offline":
            # The server dropped routing for one of this owner's agents (its host
            # socket went away). Informational here — another device may have
            # been hosting it; our own children are supervised locally.
            logger.info("server reports agent offline: %s", msg.get("agent_id"))
        else:
            return False
        return True

    def on_transport_connected(self) -> None:
        """A transport is open, but it is not yet an eligible agent host."""

        with self._lock:
            self._accepted_host_session_id = None
            self._inventory_id = None
            self._inventory_entries = {}
            self._inventory_completed = False
            if self._host_ack_timer is not None:
                self._host_ack_timer.cancel()
            self._host_ack_timer = None
        if self._spawn is None:
            timer = threading.Timer(self._host_ack_timeout, self._host_ack_timed_out)
            timer.daemon = True
            with self._lock:
                if self._accepted_host_session_id is None:
                    self._host_ack_timer = timer
                    timer.start()

    def _host_ack_timed_out(self) -> None:
        with self._lock:
            if self._accepted_host_session_id is not None:
                return
            self._host_ack_timer = None
        logger.warning("BYO host acknowledgement did not arrive within two seconds")
        self._notify(
            "This PC was not acknowledged as a personal-agent host; no agents were started.",
            "error",
        )

    def on_transport_disconnected(self) -> None:
        """Fence and settle every v2 child tied to the lost server session."""

        with self._lock:
            children = list(self._runtime_children.values())
            if self._host_ack_timer is not None:
                self._host_ack_timer.cancel()
                self._host_ack_timer = None
            self._accepted_host_session_id = None
            self._inventory_id = None
            self._inventory_entries = {}
            self._inventory_completed = False
        for child in children:
            self._kill_runtime(child, exit_kind="explicit_stop", send_exit=False)

    def on_host_registration_refused(self, msg: dict[str, Any]) -> None:
        """A refusal never creates a session and never starts retained code."""

        try:
            if set(msg) != {"type", "code", "retryable", "details", "refused_at"}:
                raise ValueError("refusal fields are invalid")
            if (
                msg["type"] != "agent_host_registration_refused"
                or msg["retryable"] is not False
                or not isinstance(msg["details"], dict)
            ):
                raise ValueError("refusal envelope is invalid")
            code = msg["code"]
            details = msg["details"]
            if code == "runtime_contract_unsupported":
                if set(details) != {
                    "required_runtime_contract_version",
                    "supported_runtime_contract_versions",
                }:
                    raise ValueError("runtime refusal details are invalid")
                required = details["required_runtime_contract_version"]
                supported = details["supported_runtime_contract_versions"]
                if (
                    type(required) is not int
                    or required <= 0
                    or not isinstance(supported, list)
                    or supported != sorted(set(supported))
                    or any(type(item) is not int or item <= 0 for item in supported)
                ):
                    raise ValueError("runtime refusal values are invalid")
            elif code == "runtime_lock_mismatch":
                if set(details) != {
                    "expected_sha256_prefix",
                    "actual_sha256_prefix",
                } or any(
                    not isinstance(value, str)
                    or re.fullmatch(r"[0-9a-f]{12}", value) is None
                    for value in details.values()
                ):
                    raise ValueError("runtime lock refusal details are invalid")
            elif code == "invalid_host_registration":
                if set(details) != {"field"} or not isinstance(details["field"], str):
                    raise ValueError("registration refusal details are invalid")
                if _SAFE_REASON.fullmatch(details["field"]) is None:
                    raise ValueError("registration refusal field is unsafe")
            else:
                raise ValueError("registration refusal code is invalid")
            _validate_utc(msg["refused_at"], "refused_at")
        except (KeyError, TypeError, ValueError):
            logger.warning("discarding malformed BYO host refusal")
            return

        with self._lock:
            # One accepted acknowledgement is authoritative for the connection;
            # a delayed refusal cannot unbind it or strand its running children.
            if self._accepted_host_session_id is not None:
                return
            if self._host_ack_timer is not None:
                self._host_ack_timer.cancel()
                self._host_ack_timer = None
            self._accepted_host_session_id = None
            self._inventory_id = None
            self._inventory_entries = {}
            self._inventory_completed = False
        logger.warning("BYO host registration refused: %s", code)
        self._notify("This PC cannot host personal agents with the installed runtime.", "error")

    def on_host_registered(self, msg: dict[str, Any]) -> bool:
        """Bind the server-issued session, then inventory before retained start."""

        try:
            if set(msg) != {
                "type",
                "host_id",
                "host_session_id",
                "inventory_required",
                "accepted_at",
            }:
                raise ValueError("ack fields are invalid")
            if msg["type"] != "agent_host_registered" or msg["host_id"] != self.host_id:
                raise ValueError("ack host is invalid")
            session_id = _canonical_uuid4(msg["host_session_id"], "host_session_id")
            if type(msg["inventory_required"]) is not bool:
                raise ValueError("inventory_required is invalid")
            _validate_utc(msg["accepted_at"], "accepted_at")
        except (KeyError, ValueError, TypeError):
            logger.warning("discarding malformed or wrong-host BYO acknowledgement")
            return False

        with self._lock:
            existing = self._accepted_host_session_id
            if existing is not None:
                return existing == session_id
            if self._host_ack_timer is not None:
                self._host_ack_timer.cancel()
                self._host_ack_timer = None
            self._accepted_host_session_id = session_id
        entries = self._local_inventory()
        if msg["inventory_required"] or entries:
            inventory_id = str(uuid.uuid4())
            with self._lock:
                self._inventory_id = inventory_id
                self._inventory_entries = {
                    (item.agent_id, item.revision_id): item for item in entries
                }
                self._inventory_completed = False
            self._send_v2_frame(
                {
                    "type": "agent_host_inventory",
                    "host_id": self.host_id,
                    "host_session_id": session_id,
                    "inventory_id": inventory_id,
                    "entries": [
                        {
                            "agent_id": item.agent_id,
                            "revision_id": item.revision_id,
                            "bundle_sha256": item.bundle_sha256,
                            "runtime_contract_version": item.runtime_contract_version,
                            "required_runtime_lock_sha256": (
                                item.required_runtime_lock_sha256
                            ),
                        }
                        for item in entries
                    ],
                }
            )
        else:
            with self._lock:
                self._inventory_completed = True
        return True

    def on_agent_registered(self, agent_id: str) -> None:
        """The server accepted a registration — disarm the silence timeout (see
        REGISTER_TIMEOUT_S). If a revision was in flight for this id, the ack is
        the rollover signal: promote the revised child and retire the old one
        (T027). The swap + kill run OFF-lock — `_kill` closes the old child's
        stdin, whose stdout pump then re-enters `_on_child_exit` and the lock."""
        promote = None
        timer = None
        with self._lock:
            pending = self._pending.get(agent_id)
            if pending is not None and not pending.registered:
                old = self._children.get(agent_id)
                pending.registered = True
                ptimer, pending.timer = pending.timer, None
                self._pending.pop(agent_id, None)
                self._children[agent_id] = pending  # inbound now routes to the new child
                promote = (old, pending, ptimer)
            else:
                child = self._children.get(agent_id)
                if child is None or child.registered:
                    return
                child.registered = True
                timer, child.timer = child.timer, None
        if promote is not None:
            old, pending, ptimer = promote
            if ptimer is not None:
                ptimer.cancel()
            self._swap_dirs(pending)   # live dir now holds the revised bundle
            if old is not None:
                self._kill(old)        # retire the old version only now
            logger.info("byo agent %s revised — rolled over to the new version", agent_id)
            self._notify(f"Your agent “{agent_id}” was updated to the new version.", "info")
            return
        if timer is not None:
            timer.cancel()
        logger.info("byo agent %s registered", agent_id)
        self._notify(f"Your agent “{agent_id}” is running on this PC.", "info")

    def on_ui_connected(self) -> None:
        """Re-send every running child's `register_agent` after a (re)connect —
        the server pops its `agents` entry on socket teardown, so without this
        the child stays alive but unreachable (contract §5).

        On the FIRST connect of a process there are no children yet: the bundles
        are on disk from an earlier session and nothing re-delivers them (the
        server only pushes `agent_bundle_deliver` from the generation path), so
        without `rehydrate()` every agent the user ever made would be permanently
        offline after the client restarts."""
        self.on_transport_connected()
        if self._spawn is None:
            # Production v2: wait for agent_host_registered and, when retained
            # entries exist, agent_host_inventory_reconciled. No disk code is
            # started merely because the WebSocket transport opened.
            return
        # Feature-058 injected-process compatibility for the existing local
        # tests only; production construction never provides a raw spawn hook.
        self.rehydrate()
        with self._lock:
            children = [c for c in self._children.values() if c.alive() and c.register_frame]
        for child in children:
            child.registered = False
            self._arm_register_timeout(child)
            self._tunnel_out(child.agent_id, child.register_frame)
        if children:
            logger.info("re-registered %d byo agent(s) after reconnect", len(children))

    def rehydrate(self) -> List[str]:
        """Start every bundle already on disk (once per host process).

        The host does NOT decide whether an agent is still allowed to run: the
        server re-authorizes at registration, so a soft-deleted or deauthorized
        agent is simply refused, goes silent, and the REGISTER_TIMEOUT_S reaper
        removes the child. That is the safe direction — refuse-by-server, not
        trust-the-disk.

        Once only: after a mid-session reconnect a child that has already exited
        must stay offline (contract §5 — "do not auto-respawn"), and a child that
        is still running is re-registered by `on_ui_connected` above.
        """
        with self._lock:
            if self._rehydrated:
                return []
            self._rehydrated = True
            known = set(self._children)

        started: List[str] = []
        try:
            entries = sorted(os.listdir(self._base_dir))
        except OSError:
            return started  # no agents root yet: nothing was ever delivered
        for agent_id in entries:
            if agent_id in known:
                continue
            if agent_id.endswith(_PENDING_SUFFIX):
                # A staging dir orphaned by a crash mid-revision: its name is not a
                # real agent id, and running it would resurrect a half-written
                # revision. Clean it up, never start it.
                self._discard_staging(os.path.join(self._base_dir, agent_id))
                continue
            directory = self._agent_dir(agent_id)
            if not directory or not os.path.isfile(os.path.join(directory, "agent_main.py")):
                continue
            self._start(agent_id, directory)
            started.append(agent_id)
        if started:
            logger.info("rehydrated %d byo agent(s) from disk: %s", len(started), started)
        return started

    # --- v2 durable inventory / immutable installation ------------------- #

    def _revision_dir(self, agent_id: str, revision_id: str) -> Optional[str]:
        agent_dir = self._agent_dir(agent_id)
        if agent_dir is None:
            return None
        try:
            _canonical_uuid4(revision_id, "revision_id")
        except ValueError:
            return None
        base = os.path.realpath(self._base_dir)
        target = os.path.realpath(
            os.path.join(agent_dir, "revisions", revision_id)
        )
        try:
            if os.path.commonpath([base, target]) != base:
                return None
        except ValueError:
            return None
        return target

    @staticmethod
    def _read_json(path: str) -> Optional[dict[str, Any]]:
        try:
            with open(path, encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def _installed_revision(
        self, agent_id: str, revision_id: str
    ) -> Optional[_InstalledRevision]:
        directory = self._revision_dir(agent_id, revision_id)
        if directory is None or not os.path.isdir(directory):
            return None
        metadata = self._read_json(os.path.join(directory, _RUNTIME_METADATA_FILE))
        if metadata is None or set(metadata) != {
            "agent_id",
            "revision_id",
            "bundle_sha256",
            "runtime_contract_version",
            "required_runtime_lock_sha256",
        }:
            return None
        if metadata.get("agent_id") != agent_id or metadata.get("revision_id") != revision_id:
            return None
        compatibility = check_runtime_compatibility(
            metadata.get("runtime_contract_version"),
            metadata.get("required_runtime_lock_sha256"),
        )
        digest = metadata.get("bundle_sha256")
        if compatibility is not None or not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            return None
        try:
            if set(os.listdir(directory)) != set(BUNDLE_FILE_NAMES) | {
                _RUNTIME_METADATA_FILE
            }:
                return None
            files = {}
            for name in BUNDLE_FILE_NAMES:
                with open(os.path.join(directory, name), encoding="utf-8") as handle:
                    files[name] = handle.read()
        except OSError:
            return None
        if canonical_bundle_sha256(files) != digest:
            return None
        return _InstalledRevision(
            agent_id=agent_id,
            revision_id=revision_id,
            directory=directory,
            bundle_sha256=digest,
            runtime_contract_version=metadata["runtime_contract_version"],
            required_runtime_lock_sha256=metadata["required_runtime_lock_sha256"],
        )

    def _local_inventory(self) -> list[_InstalledRevision]:
        entries: list[_InstalledRevision] = []
        try:
            agent_ids = sorted(os.listdir(self._base_dir))
        except OSError:
            return entries
        for agent_id in agent_ids:
            agent_dir = self._agent_dir(agent_id)
            if agent_dir is None:
                continue
            revisions_dir = os.path.join(agent_dir, "revisions")
            try:
                revision_ids = sorted(os.listdir(revisions_dir))
            except OSError:
                continue
            for revision_id in revision_ids:
                installed = self._installed_revision(agent_id, revision_id)
                if installed is not None:
                    entries.append(installed)
                else:
                    # Corrupt/partial v2 revisions are never asserted as valid
                    # inventory and can never become executable after reconnect.
                    candidate = os.path.join(revisions_dir, revision_id)
                    if os.path.isdir(candidate):
                        shutil.rmtree(candidate, ignore_errors=True)
                        _fsync_directory(revisions_dir)
        return entries

    @staticmethod
    def _write_fsynced(path: str, value: str) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())

    def _install_v2_bundle(
        self,
        *,
        agent_id: str,
        revision_id: str,
        files: dict[str, str],
        bundle_sha256: str,
        runtime_contract_version: int,
        required_runtime_lock_sha256: str,
    ) -> Optional[_InstalledRevision]:
        directory = self._revision_dir(agent_id, revision_id)
        if directory is None:
            return None
        existing = self._installed_revision(agent_id, revision_id)
        if existing is not None:
            if (
                existing.bundle_sha256 == bundle_sha256
                and existing.runtime_contract_version == runtime_contract_version
                and existing.required_runtime_lock_sha256
                == required_runtime_lock_sha256
            ):
                return existing
            logger.warning("refusing replacement of immutable BYO revision %s", revision_id)
            return None
        if os.path.exists(directory):
            logger.warning("refusing malformed immutable BYO revision %s", revision_id)
            return None

        staging_root = os.path.join(self._base_dir, ".staging")
        staging = os.path.join(staging_root, f"{revision_id}-{uuid.uuid4()}")
        metadata = {
            "agent_id": agent_id,
            "revision_id": revision_id,
            "bundle_sha256": bundle_sha256,
            "runtime_contract_version": runtime_contract_version,
            "required_runtime_lock_sha256": required_runtime_lock_sha256,
        }
        try:
            os.makedirs(staging_root, exist_ok=True)
            os.mkdir(staging, 0o700)
            for name in BUNDLE_FILE_NAMES:
                self._write_fsynced(os.path.join(staging, name), files[name])
            self._write_fsynced(
                os.path.join(staging, _RUNTIME_METADATA_FILE),
                json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n",
            )
            _fsync_directory(staging)
            os.makedirs(os.path.dirname(directory), exist_ok=True)
            _atomic_install_rename(staging, directory)
            _fsync_directory(os.path.dirname(directory))
            _fsync_directory(staging_root)
        except (OSError, KeyError):
            logger.exception("could not atomically install BYO revision %s", revision_id)
            self._notify("Couldn't install a personal-agent revision on this PC.", "error")
            return None
        finally:
            if os.path.isdir(staging):
                shutil.rmtree(staging, ignore_errors=True)
        return self._installed_revision(agent_id, revision_id)

    @staticmethod
    def _prelaunch_fence(value: object) -> dict[str, Any]:
        expected = {
            "agent_id",
            "host_id",
            "host_session_id",
            "delivery_id",
            "revision_id",
            "runtime_instance_id",
            "lifecycle_generation",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("pre-launch fence fields are invalid")
        if not isinstance(value["agent_id"], str) or not _SAFE_ID.match(value["agent_id"]):
            raise ValueError("agent_id is invalid")
        for name in (
            "host_id",
            "host_session_id",
            "delivery_id",
            "revision_id",
            "runtime_instance_id",
        ):
            _canonical_uuid4(value[name], name)
        generation = value["lifecycle_generation"]
        if type(generation) is not int or generation < 0 or generation >= 1 << 64:
            raise ValueError("lifecycle_generation is invalid")
        return dict(value)

    @staticmethod
    def _full_fence(value: object) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != {
            "agent_id",
            "host_id",
            "host_session_id",
            "delivery_id",
            "revision_id",
            "runtime_instance_id",
            "process_id",
            "lifecycle_generation",
        }:
            raise ValueError("runtime fence fields are invalid")
        prelaunch = dict(value)
        process_id = prelaunch.pop("process_id")
        ByoAgentHost._prelaunch_fence(prelaunch)
        _canonical_uuid4(process_id, "process_id")
        return dict(value)

    def _deliver_v2(self, msg: dict[str, Any]) -> bool:
        with self._lock:
            session_id = self._accepted_host_session_id
            inventory_completed = self._inventory_completed
        if session_id is None:
            logger.warning("dropping BYO bundle before agent_host_registered")
            return False
        if not inventory_completed:
            logger.warning("dropping BYO bundle before host inventory reconciliation")
            return False
        try:
            if set(msg) != {
                "type",
                "fence",
                "runtime_contract_version",
                "required_runtime_lock_sha256",
                "bundle_sha256",
                "files",
            }:
                raise ValueError("delivery fields are invalid")
            fence = self._prelaunch_fence(msg["fence"])
            if fence["host_id"] != self.host_id or fence["host_session_id"] != session_id:
                raise ValueError("delivery host session is stale")
            compatibility = check_runtime_compatibility(
                msg["runtime_contract_version"],
                msg["required_runtime_lock_sha256"],
            )
            if compatibility is not None:
                logger.warning("refusing incompatible BYO bundle: %s", compatibility)
                return False
            files = msg["files"]
            digest = msg["bundle_sha256"]
            if not isinstance(files, dict) or not isinstance(digest, str):
                raise ValueError("bundle fields are invalid")
            if _SHA256.fullmatch(digest) is None or canonical_bundle_sha256(files) != digest:
                logger.warning("refusing BYO bundle with bundle_digest_mismatch")
                return False
        except (KeyError, TypeError, ValueError):
            logger.warning("discarding malformed v2 BYO bundle", exc_info=True)
            return False

        with self._lock:
            if any(
                child.fence["runtime_instance_id"] == fence["runtime_instance_id"]
                for child in self._runtime_children.values()
            ):
                return True
        installed = self._install_v2_bundle(
            agent_id=fence["agent_id"],
            revision_id=fence["revision_id"],
            files=files,
            bundle_sha256=digest,
            runtime_contract_version=msg["runtime_contract_version"],
            required_runtime_lock_sha256=msg["required_runtime_lock_sha256"],
        )
        if installed is None:
            self._prelaunch_failure(
                fence,
                runtime_contract_version=msg["runtime_contract_version"],
                bundle_sha256=digest,
                reason_code="bundle_install_failed",
            )
            return False
        return self._launch_v2(installed, fence)

    def _selected_delivery(
        self, value: object, installed: _InstalledRevision
    ) -> dict[str, Any]:
        expected = {
            "delivery_id",
            "runtime_instance_id",
            "lifecycle_generation",
            "runtime_contract_version",
            "required_runtime_lock_sha256",
            "bundle_sha256",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("selected delivery fields are invalid")
        _canonical_uuid4(value["delivery_id"], "delivery_id")
        _canonical_uuid4(value["runtime_instance_id"], "runtime_instance_id")
        generation = value["lifecycle_generation"]
        if type(generation) is not int or generation < 0 or generation >= 1 << 64:
            raise ValueError("selected lifecycle generation is invalid")
        if (
            value["runtime_contract_version"] != installed.runtime_contract_version
            or value["required_runtime_lock_sha256"]
            != installed.required_runtime_lock_sha256
            or value["bundle_sha256"] != installed.bundle_sha256
        ):
            raise ValueError("selected delivery does not match the retained revision")
        return dict(value)

    def _reconcile_inventory(self, msg: dict[str, Any]) -> bool:
        """Validate the complete action set before deleting or starting anything."""

        with self._lock:
            session_id = self._accepted_host_session_id
            inventory_id = self._inventory_id
            entries = dict(self._inventory_entries)
        try:
            if set(msg) != {
                "type",
                "host_id",
                "host_session_id",
                "inventory_id",
                "actions",
                "reconciled_at",
            }:
                raise ValueError("inventory response fields are invalid")
            if (
                msg["host_id"] != self.host_id
                or msg["host_session_id"] != session_id
                or msg["inventory_id"] != inventory_id
                or session_id is None
                or inventory_id is None
            ):
                raise ValueError("inventory response fence is stale")
            _validate_utc(msg["reconciled_at"], "reconciled_at")
            actions = msg["actions"]
            if not isinstance(actions, list) or len(actions) != len(entries):
                raise ValueError("inventory response is incomplete")
            validated: list[tuple[_InstalledRevision, str, Optional[dict[str, Any]]]] = []
            seen: set[tuple[str, str]] = set()
            for action in actions:
                if not isinstance(action, dict) or set(action) != {
                    "agent_id",
                    "revision_id",
                    "action",
                    "reason_code",
                    "selected_delivery",
                }:
                    raise ValueError("inventory action fields are invalid")
                key = (action["agent_id"], action["revision_id"])
                if key in seen or key not in entries:
                    raise ValueError("inventory action is duplicate or unknown")
                seen.add(key)
                decision = action["action"]
                if decision not in {"keep_stopped", "start", "delete"}:
                    raise ValueError("inventory action is invalid")
                reason = action["reason_code"]
                if reason is not None and (
                    not isinstance(reason, str) or _SAFE_REASON.fullmatch(reason) is None
                ):
                    raise ValueError("inventory reason code is invalid")
                selected = action["selected_delivery"]
                if decision == "start":
                    selected = self._selected_delivery(selected, entries[key])
                elif selected is not None:
                    raise ValueError("only start may carry a selected delivery")
                validated.append((entries[key], decision, selected))
            if seen != set(entries):
                raise ValueError("inventory response omitted an entry")
        except (KeyError, TypeError, ValueError):
            logger.warning("discarding invalid BYO inventory reconciliation", exc_info=True)
            return False

        # Apply all stop/delete decisions before launching any selected entry.
        for installed, decision, _selected in validated:
            with self._lock:
                running = [
                    child
                    for child in self._runtime_children.values()
                    if child.installed.agent_id == installed.agent_id
                    and child.installed.revision_id == installed.revision_id
                ]
            if decision in {"keep_stopped", "delete"}:
                for child in running:
                    self._kill_runtime(child, exit_kind="explicit_stop")
            if decision == "delete":
                shutil.rmtree(installed.directory, ignore_errors=True)
                _fsync_directory(os.path.dirname(installed.directory))
        with self._lock:
            self._inventory_completed = True
            self._inventory_id = None
            self._inventory_entries = {}
        for installed, decision, selected in validated:
            if decision != "start" or selected is None:
                continue
            fence = {
                "agent_id": installed.agent_id,
                "host_id": self.host_id,
                "host_session_id": session_id,
                "delivery_id": selected["delivery_id"],
                "revision_id": installed.revision_id,
                "runtime_instance_id": selected["runtime_instance_id"],
                "lifecycle_generation": selected["lifecycle_generation"],
            }
            self._launch_v2(installed, fence)
        return True

    # --- v2 launch, child protocol, heartbeat, and exit ------------------ #

    def _send_v2_frame(self, frame: dict[str, Any]) -> None:
        try:
            self._send_frame(frame)
        except Exception:  # noqa: BLE001 - transport loss triggers host teardown
            logger.debug("BYO v2 frame send failed: %s", frame.get("type"), exc_info=True)

    def _prelaunch_failure(
        self,
        fence: dict[str, Any],
        *,
        runtime_contract_version: int,
        bundle_sha256: str,
        reason_code: str,
    ) -> None:
        """Report a valid selected delivery that failed before process bind."""

        with self._lock:
            if self._accepted_host_session_id != fence.get("host_session_id"):
                return
        self._send_v2_frame(
            {
                "type": "agent_runtime_state",
                "fence": dict(fence),
                "state": "failed",
                "runtime_contract_version": runtime_contract_version,
                "bundle_sha256": bundle_sha256,
                "observed_at": _utc_now(),
                "reason_code": reason_code,
            }
        )

    def _runtime_state(
        self, child: _RuntimeChild, state: str, reason_code: Optional[str] = None
    ) -> None:
        with self._lock:
            if self._accepted_host_session_id != child.fence["host_session_id"]:
                return
        self._send_v2_frame(
            {
                "type": "agent_runtime_state",
                "fence": dict(child.fence),
                "state": state,
                "runtime_contract_version": child.installed.runtime_contract_version,
                "bundle_sha256": child.installed.bundle_sha256,
                "observed_at": _utc_now(),
                "reason_code": reason_code,
            }
        )

    def _launch_v2(
        self, installed: _InstalledRevision, prelaunch_fence: dict[str, Any]
    ) -> bool:
        runtime_instance_id = prelaunch_fence["runtime_instance_id"]
        with self._lock:
            if prelaunch_fence["host_session_id"] != self._accepted_host_session_id:
                return False
            if runtime_instance_id in self._launching_runtime_instances or any(
                child.fence["runtime_instance_id"] == runtime_instance_id
                for child in self._runtime_children.values()
            ):
                return True
            self._launching_runtime_instances.add(runtime_instance_id)

        def release_launch() -> None:
            with self._lock:
                self._launching_runtime_instances.discard(runtime_instance_id)

        holder: dict[str, _RuntimeChild] = {}
        bound = threading.Event()

        def current() -> Optional[_RuntimeChild]:
            if not bound.wait(self._register_timeout):
                return None
            return holder.get("child")

        def stdout_line(line: bytes) -> None:
            child = current()
            if child is not None:
                self._on_runtime_stdout(child, line)

        def stderr_line(line: bytes) -> None:
            child = current()
            if child is not None and line:
                logger.info("BYO %s stderr: %r", child.agent_id, line[:120])

        def diagnostic(code: str, stream: OutputStream) -> None:
            child = current()
            if child is not None:
                logger.warning("BYO %s %s on %s", child.agent_id, code, stream.value)

        def stream_eof(stream: OutputStream) -> None:
            child = current()
            if child is not None and stream is OutputStream.STDOUT:
                self._on_runtime_protocol_eof(child)

        def exited(_process, snapshot) -> None:
            child = current()
            if child is not None:
                self._on_runtime_exit(child, snapshot)

        environment = _child_environment()
        process_id = self._process_id_factory()
        if not isinstance(process_id, uuid.UUID) or process_id.version != 4:
            logger.error("BYO process_id factory returned a non-UUID4")
            self._prelaunch_failure(
                prelaunch_fence,
                runtime_contract_version=installed.runtime_contract_version,
                bundle_sha256=installed.bundle_sha256,
                reason_code="child_start_failed",
            )
            release_launch()
            return False
        fence = dict(prelaunch_fence, process_id=str(process_id))
        environment.update(
            {
                "ASTRAL_RUNTIME_FENCE_JSON": json.dumps(
                    fence, sort_keys=True, separators=(",", ":")
                ),
                "ASTRAL_RUNTIME_CONTRACT_VERSION": str(
                    installed.runtime_contract_version
                ),
                "ASTRAL_RUNTIME_BUNDLE_SHA256": installed.bundle_sha256,
            }
        )
        try:
            supervised = self._process_supervisor.spawn(
                process_id=process_id,
                argv=worker_argv(installed.directory),
                cwd=installed.directory,
                env=environment,
                process_factory=self._spawn,
                on_stdout_line=stdout_line,
                on_stderr_line=stderr_line,
                on_diagnostic=diagnostic,
                on_stream_eof=stream_eof,
                on_exit=exited,
            )
        except Exception:  # noqa: BLE001 - spawn failure is user-visible
            logger.exception("could not start v2 BYO worker %s", installed.agent_id)
            self._notify("Couldn't start a personal-agent worker on this PC.", "error")
            self._prelaunch_failure(
                prelaunch_fence,
                runtime_contract_version=installed.runtime_contract_version,
                bundle_sha256=installed.bundle_sha256,
                reason_code="child_start_failed",
            )
            bound.set()
            release_launch()
            return False
        child = _RuntimeChild(
            installed=installed,
            fence=fence,
            supervised=supervised,
        )
        holder["child"] = child
        with self._lock:
            self._runtime_children[str(process_id)] = child
        timer = threading.Timer(
            self._register_timeout,
            self._runtime_registration_timed_out,
            args=(child,),
        )
        timer.daemon = True
        child.register_timer = timer
        timer.start()
        self._runtime_state(child, "starting")
        bound.set()
        release_launch()
        logger.info("started fenced BYO agent %s (pid=%s)", child.agent_id, child.proc.pid)
        return True

    @staticmethod
    def _valid_agent_card(value: object, agent_id: str) -> bool:
        if not isinstance(value, dict) or set(value) != {
            "name",
            "description",
            "agent_id",
            "version",
            "skills",
            "metadata",
        }:
            return False
        return (
            isinstance(value["name"], str)
            and bool(value["name"].strip())
            and isinstance(value["description"], str)
            and value["agent_id"] == agent_id
            and isinstance(value["version"], str)
            and re.fullmatch(
                r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)",
                value["version"],
            )
            is not None
            and isinstance(value["skills"], list)
            and isinstance(value["metadata"], dict)
        )

    @staticmethod
    def _valid_request_identity(value: dict[str, Any]) -> bool:
        try:
            _canonical_uuid4(value.get("request_id"), "request_id")
            _canonical_uuid4(
                value.get("request_generation"), "request_generation"
            )
        except ValueError:
            return False
        return True

    def _on_runtime_stdout(self, child: _RuntimeChild, line: bytes) -> None:
        with self._lock:
            if (
                self._runtime_children.get(child.fence["process_id"]) is not child
                or self._accepted_host_session_id != child.fence["host_session_id"]
            ):
                return
        try:
            frame = json.loads(line.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, ValueError, TypeError):
            logger.warning("BYO %s emitted a non-JSON protocol line", child.agent_id)
            return
        if not isinstance(frame, dict):
            self._fail_runtime_registration(child)
            return
        if not child.registered:
            child.protocol_frame_seen = True
            if (
                set(frame) != {
                    "type",
                    "fence",
                    "runtime_contract_version",
                    "bundle_sha256",
                    "agent_card",
                }
                or frame.get("type") != "agent_runtime_register"
                or frame.get("fence") != child.fence
                or frame.get("runtime_contract_version")
                != child.installed.runtime_contract_version
                or frame.get("bundle_sha256") != child.installed.bundle_sha256
                or not self._valid_agent_card(frame.get("agent_card"), child.agent_id)
            ):
                self._fail_runtime_registration(child)
                return
            child.registered = True
            child.ready = True
            if child.register_timer is not None:
                child.register_timer.cancel()
                child.register_timer = None
            self._send_v2_frame(frame)
            self._emit_runtime_heartbeat(child)
            self._runtime_state(child, "ready")
            return
        if frame.get("type") == "agent_runtime_register":
            # Exact repeats are idempotent; mismatched repeats are stale.
            if (
                set(frame) == {
                    "type",
                    "fence",
                    "runtime_contract_version",
                    "bundle_sha256",
                    "agent_card",
                }
                and
                frame.get("fence") == child.fence
                and frame.get("runtime_contract_version")
                == child.installed.runtime_contract_version
                and frame.get("bundle_sha256") == child.installed.bundle_sha256
                and self._valid_agent_card(frame.get("agent_card"), child.agent_id)
            ):
                self._send_v2_frame(frame)
            return
        if frame.get("type") == "agent_runtime_heartbeat":
            sequence = frame.get("heartbeat_sequence")
            if (
                set(frame) != {"type", "fence", "heartbeat_sequence"}
                or frame.get("fence") != child.fence
                or type(sequence) is not int
                or sequence <= child.last_heartbeat_sequence
                or sequence >= 1 << 64
            ):
                return
            child.last_heartbeat_sequence = sequence
            self._send_v2_frame(frame)
            if not child.ready:
                child.ready = True
                if child.register_timer is not None:
                    child.register_timer.cancel()
                    child.register_timer = None
                self._runtime_state(child, "ready")
            return
        if frame.get("fence") != child.fence:
            logger.warning("dropping stale child frame for %s", child.agent_id)
            return
        if not self._valid_request_identity(frame):
            logger.warning("dropping unfenced request result for %s", child.agent_id)
            return
        self._send_v2_frame(frame)

    def _emit_runtime_heartbeat(self, child: _RuntimeChild) -> None:
        """Emit and re-arm the host-owned monotonic liveness heartbeat."""

        with self._lock:
            if (
                self._runtime_children.get(child.fence["process_id"]) is not child
                or self._accepted_host_session_id != child.fence["host_session_id"]
                or not child.registered
                or not child.ready
                or not child.alive()
            ):
                return
            child.last_heartbeat_sequence += 1
            sequence = child.last_heartbeat_sequence
        self._send_v2_frame(
            {
                "type": "agent_runtime_heartbeat",
                "fence": dict(child.fence),
                "heartbeat_sequence": sequence,
            }
        )
        timer = threading.Timer(
            self._heartbeat_interval,
            self._emit_runtime_heartbeat,
            args=(child,),
        )
        timer.daemon = True
        child.heartbeat_timer = timer
        timer.start()

    def _runtime_registration_timed_out(self, child: _RuntimeChild) -> None:
        if child.ready or not child.alive():
            return
        self._runtime_state(child, "failed", "child_registration_timeout")
        self._kill_runtime(child, exit_kind="explicit_stop")

    def _fail_runtime_registration(self, child: _RuntimeChild) -> None:
        if child.registered or not child.alive():
            return
        self._runtime_state(child, "failed", "child_registration_timeout")
        threading.Thread(
            target=self._kill_runtime,
            args=(child,),
            kwargs={"exit_kind": "explicit_stop"},
            name=f"byo-invalid-register-{child.fence['process_id']}",
            daemon=True,
        ).start()

    def _on_runtime_protocol_eof(self, child: _RuntimeChild) -> None:
        def settle() -> None:
            time.sleep(0.01)
            if child.alive():
                self._kill_runtime(child, exit_kind="protocol_eof")

        threading.Thread(
            target=settle,
            name=f"byo-protocol-eof-{child.fence['process_id']}",
            daemon=True,
        ).start()

    def _on_runtime_exit(self, child: _RuntimeChild, snapshot) -> None:
        with self._lock:
            if self._runtime_children.get(child.fence["process_id"]) is child:
                self._runtime_children.pop(child.fence["process_id"], None)
        if child.register_timer is not None:
            child.register_timer.cancel()
            child.register_timer = None
        if child.heartbeat_timer is not None:
            child.heartbeat_timer.cancel()
            child.heartbeat_timer = None
        with child.exit_lock:
            if child.exit_sent:
                return
            child.exit_sent = True
            if child.suppress_exit:
                return
            exit_kind = child.requested_exit_kind or "process_exit"
            exit_code = snapshot.exit_code if exit_kind == "process_exit" else None
            if exit_kind == "process_exit" and type(exit_code) is not int:
                exit_kind = "protocol_eof"
                exit_code = None
            self._send_v2_frame(
                {
                    "type": "agent_runtime_exit",
                    "fence": dict(child.fence),
                    "exit_kind": exit_kind,
                    "exit_code": exit_code,
                }
            )

    def _kill_runtime(
        self,
        child: _RuntimeChild,
        *,
        exit_kind: str,
        send_exit: bool = True,
    ) -> None:
        if child.requested_exit_kind is None:
            child.requested_exit_kind = exit_kind
        if not send_exit:
            child.suppress_exit = True
        reason = (
            TerminationReason.QUIT
            if exit_kind == "explicit_stop"
            else TerminationReason.FAILURE
        )
        try:
            child.supervised.terminate(reason=reason)
        except Exception:  # noqa: BLE001
            logger.exception("could not settle BYO process tree %s", child.fence["process_id"])

    def _stop_runtime_fence(self, fence: object) -> bool:
        try:
            expected = self._full_fence(fence)
        except ValueError:
            return False
        with self._lock:
            child = self._runtime_children.get(expected["process_id"])
        if child is None or child.fence != expected:
            return False
        self._kill_runtime(child, exit_kind="explicit_stop")
        return True

    def _to_runtime_child(self, msg: dict[str, Any]) -> None:
        try:
            fence = self._full_fence(msg.get("fence"))
        except ValueError:
            return
        with self._lock:
            child = self._runtime_children.get(fence["process_id"])
        if child is None or child.fence != fence or not child.alive():
            return
        frame = msg.get("frame")
        if frame is None:
            return
        if isinstance(frame, str):
            try:
                parsed = json.loads(frame)
            except (ValueError, TypeError):
                return
        else:
            parsed = frame
        if (
            not isinstance(parsed, dict)
            or parsed.get("fence") != fence
            or not self._valid_request_identity(parsed)
        ):
            return
        text = frame if isinstance(frame, str) else json.dumps(frame)
        try:
            child.supervised.write_line(text)
        except (OSError, ValueError, BrokenPipeError):
            logger.warning("BYO %s stdin closed; request dropped", child.agent_id)

    # --- lifecycle --------------------------------------------------------- #

    def deliver(self, agent_id: str, files: dict, constitution_version=None) -> Optional[str]:
        """Write a delivered bundle and start it. Returns the agent dir, or None.

        A delivery for an agent that is CURRENTLY RUNNING is a revision: the new
        bundle is staged and started alongside the live one, which keeps serving
        until the revised child registers (T027, `_deliver_revision`). Otherwise
        (first delivery, or the previous child already exited) it replaces in place.
        """
        directory = self._agent_dir(agent_id)
        if directory is None:
            logger.warning("refusing bundle with unusable agent_id %r", agent_id)
            return None
        if not isinstance(files, dict) or not files:
            logger.warning("refusing empty bundle for %s", agent_id)
            return None

        with self._lock:
            live = self._children.get(agent_id)
            is_revision = live is not None and live.alive()
        if is_revision:
            return self._deliver_revision(agent_id, files, directory)

        self.stop(agent_id)              # replace a dead/leftover child, if any
        self._discard_pending(agent_id)  # drop any stray in-flight revision
        if not self._write_bundle(agent_id, directory, files):
            return None
        logger.info("wrote %d bundle file(s) for %s -> %s", len(files), agent_id, directory)
        self._start(agent_id, directory)
        return directory

    def _deliver_revision(self, agent_id: str, files: dict, live_dir: str) -> Optional[str]:
        """Stage a revision beside the running agent and start it WITHOUT touching
        the live child; the swap happens later, on the revised child's ack
        (`on_agent_registered`), or is discarded on timeout (T027)."""
        self._discard_pending(agent_id)      # supersede an earlier in-flight revision
        staging = live_dir + _PENDING_SUFFIX
        self._discard_staging(staging)       # clear a leftover staging dir
        if not self._write_bundle(agent_id, staging, files):
            return None
        logger.info("staged a revision of %s (%d file(s)) -> %s", agent_id, len(files), staging)
        self._start(agent_id, staging, pending=True)
        return staging

    def _write_bundle(self, agent_id: str, directory: str, files: dict) -> bool:
        """Write a flat {filename: source} bundle into `directory`. Returns success."""
        try:
            os.makedirs(directory, exist_ok=True)
            for name, source in files.items():
                # The bundle is a FLAT {filename: source} map; anything that tries
                # to escape the agent's own directory is not a filename.
                if not isinstance(name, str) or not isinstance(source, str):
                    continue
                if os.path.basename(name) != name or name in (".", ".."):
                    logger.warning("skipping bundle entry with a path in it: %r", name)
                    continue
                with open(os.path.join(directory, name), "w", encoding="utf-8") as fh:
                    fh.write(source)
            return True
        except OSError:
            logger.exception("could not write the bundle for %s", agent_id)
            self._notify(f"Couldn't save your agent “{agent_id}” to this PC.", "error")
            return False

    def _start(self, agent_id: str, directory: str, pending: bool = False) -> None:
        holder: dict[str, _Child] = {}
        bound = threading.Event()

        def current() -> Optional[_Child]:
            if not bound.wait(self._register_timeout):
                return None
            return holder.get("child")

        def stdout_line(line: bytes) -> None:
            child = current()
            if child is not None:
                self._on_legacy_stdout_line(child, line)

        def stderr_line(line: bytes) -> None:
            child = current()
            if child is not None and line:
                logger.info("byo %s: %s", child.agent_id, line[:120].decode(
                    "utf-8", errors="replace"
                ))

        def exited(_process, _snapshot) -> None:
            child = current()
            if child is not None:
                self._on_child_exit(child)

        try:
            supervised = self._process_supervisor.spawn(
                process_id=uuid.uuid4(),
                argv=worker_argv(directory),
                cwd=directory,
                process_factory=self._spawn,
                on_stdout_line=stdout_line,
                on_stderr_line=stderr_line,
                on_stream_eof=lambda _stream: None,
                on_exit=exited,
            )
        except Exception:  # noqa: BLE001 — a failed spawn is a user-visible failure
            logger.exception("could not start the worker for %s", agent_id)
            self._notify(f"Couldn't start your agent “{agent_id}”.", "error")
            bound.set()
            return

        child = _Child(
            agent_id,
            supervised.raw_process,
            directory,
            supervised=supervised,
        )
        holder["child"] = child
        with self._lock:
            (self._pending if pending else self._children)[agent_id] = child

        # Armed at spawn, not at the first stdout frame: a child that dies before
        # it ever writes `register_agent` must fail here too, not hang forever.
        self._arm_register_timeout(child)
        bound.set()
        logger.info("started byo agent %s (pid=%s)%s", agent_id,
                    getattr(child.proc, "pid", "?"),
                    " [revision, staged]" if pending else "")

    def stop(self, agent_id: str) -> bool:
        """Terminate one child and forget it. Idempotent."""
        with self._lock:
            child = self._children.pop(agent_id, None)
        if child is None:
            return False
        self._kill(child)
        logger.info("stopped byo agent %s", agent_id)
        return True

    def remove(self, agent_id: str) -> bool:
        """Terminate one child AND delete its bundle from disk (server `agent_stop`).

        Distinct from `stop()`, which is the internal "replace this child" used by
        re-delivery and by client shutdown — those must KEEP the bundle.
        """
        self._discard_pending(agent_id)  # a deleted agent kills any in-flight revision too
        stopped = self.stop(agent_id)
        directory = self._agent_dir(agent_id)
        if directory and os.path.isdir(directory):
            try:
                shutil.rmtree(directory)
                logger.info("removed the bundle for %s", agent_id)
            except OSError:
                logger.exception("could not remove the bundle dir for %s", agent_id)
        return stopped

    def stop_all(self) -> None:
        """Client is closing: every user agent dies with it (contract §5) — the
        server sees the socket drop and takes them honestly offline. In-flight
        revision children die too (else a mid-revision close orphans one), and
        their staging dirs are cleaned up; live bundles stay on disk."""
        with self._lock:
            if self._host_ack_timer is not None:
                self._host_ack_timer.cancel()
                self._host_ack_timer = None
            children = list(self._children.values())
            pending = list(self._pending.values())
            runtime_children = list(self._runtime_children.values())
            self._children.clear()
            self._pending.clear()
        for child in children + pending:
            self._kill(child)
        for child in runtime_children:
            self._kill_runtime(child, exit_kind="explicit_stop")
        for child in pending:
            self._discard_staging(child.dir)
        if children or pending or runtime_children:
            logger.info("stopped %d byo agent(s)%s on client close",
                        len(children) + len(runtime_children),
                        f" (+{len(pending)} in-flight revision)" if pending else "")

    def running(self) -> List[str]:
        with self._lock:
            legacy = [a for a, c in self._children.items() if c.alive()]
            runtime = [
                child.agent_id
                for child in self._runtime_children.values()
                if child.alive()
            ]
        return list(dict.fromkeys(legacy + runtime))

    # --- revision staging (T027) ------------------------------------------- #

    def _discard_pending(self, agent_id: str) -> None:
        """Drop any in-flight revision for `agent_id`: kill its child and remove
        the staging dir. Used when a newer revision supersedes it, on delete, and
        before a fresh (non-revision) delivery."""
        with self._lock:
            child = self._pending.pop(agent_id, None)
        if child is None:
            return
        self._kill(child)
        self._discard_staging(child.dir)

    def _discard_staging(self, staging: str) -> None:
        """Remove a `.pending` staging dir. The suffix guard is a safety net: this
        must never be able to delete a live bundle."""
        if staging and staging.endswith(_PENDING_SUFFIX) and os.path.isdir(staging):
            try:
                shutil.rmtree(staging)
            except OSError:
                logger.exception("could not remove staging dir %s", staging)

    def _swap_dirs(self, pending: _Child) -> None:
        """Promote the staged revision on disk: replace the live bundle dir with
        the staging dir the (now registered) revised child runs from. The child
        imported its code at start-up, so it holds no handle on the dir and the
        rename is safe even while it runs; afterwards the live dir name matches
        the agent_id again, so rehydrate() finds it on the next launch."""
        live_dir = self._agent_dir(pending.agent_id)
        staging = pending.dir
        if not live_dir or staging == live_dir:
            return
        try:
            if os.path.isdir(live_dir):
                shutil.rmtree(live_dir)
            os.replace(staging, live_dir)   # atomic within the agents root
            pending.dir = live_dir
        except OSError:
            logger.exception("byo %s: could not swap in the revised bundle", pending.agent_id)

    # --- pipes ------------------------------------------------------------- #

    def _on_legacy_stdout_line(self, child: _Child, raw_line: bytes) -> None:
        """One bounded feature-058 child line -> its legacy tunnel envelope."""

        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            return
        try:
            frame = json.loads(line)
        except (ValueError, TypeError):
            logger.warning("byo %s: discarding non-JSON stdout line: %.120s",
                           child.agent_id, line)
            return
        if not isinstance(frame, dict):
            logger.warning("byo %s: discarding non-object stdout frame", child.agent_id)
            return
        if frame.get("type") == "register_agent":
            card = frame.get("agent_card")
            card_id = card.get("agent_id") if isinstance(card, dict) else None
            if card_id != child.agent_id:
                logger.warning(
                    "byo %s: dropping register_agent for a different agent_id %r",
                    child.agent_id, card_id,
                )
                return
            child.register_frame = json.dumps(frame)
        self._tunnel_out(child.agent_id, json.dumps(frame))

    def _to_child(self, agent_id: str, frame) -> None:
        """An inbound `agent_tunnel` push -> the child's stdin, one JSON line."""
        with self._lock:
            child = self._children.get(agent_id)
        if child is None or not child.alive() or child.supervised is None:
            logger.warning("agent_tunnel for %s with no running child — dropped", agent_id)
            return
        if frame is None:
            return
        text = frame if isinstance(frame, str) else json.dumps(frame)
        try:
            child.supervised.write_line(text)
        except (OSError, ValueError, BrokenPipeError):
            logger.warning("byo %s: stdin closed — dropping frame", agent_id)

    def _tunnel_out(self, agent_id: str, frame: str) -> None:
        """Wrap one agent frame in the C->S `agent_tunnel` ui_event (contract §7)."""
        try:
            self._send_event("agent_tunnel", {
                "agent_id": agent_id,
                "frame": frame,
                "host_session_id": self.host_session_id,
            })
        except Exception:  # noqa: BLE001 — a dead socket must not kill the pump
            logger.debug("agent_tunnel send failed for %s", agent_id, exc_info=True)

    # --- failure handling --------------------------------------------------- #

    def _arm_register_timeout(self, child: _Child) -> None:
        timer = threading.Timer(
            self._register_timeout, self._registration_timed_out, args=(child,)
        )
        timer.daemon = True
        with self._lock:
            if child.registered:
                return  # already acked before we could arm
            child.timer = timer
        timer.start()

    def _registration_timed_out(self, child: _Child) -> None:
        agent_id = child.agent_id
        with self._lock:
            if child.registered:
                return
            if self._pending.get(agent_id) is child:
                self._pending.pop(agent_id, None)
                is_pending = True
            elif self._children.get(agent_id) is child:
                self._children.pop(agent_id, None)
                is_pending = False
            else:
                return  # already removed / promoted / replaced deliberately
        self._kill(child)
        if is_pending:
            # A revision that never registered: keep the version the owner is
            # relying on, drop only the staged one (T027 — a failed revision must
            # never take the running agent down).
            self._discard_staging(child.dir)
            logger.warning("byo %s: revision not accepted in time — kept the running version",
                           agent_id)
            self._notify(
                f"Your update to “{agent_id}” wasn't accepted; the previous version is "
                "still running.",
                "warning",
            )
            return
        logger.warning("byo %s: no agent_registered ack — reaped the child", agent_id)
        self._notify(
            f"Your agent “{agent_id}” couldn't start: the server didn't accept it. "
            "Open your agents and try again.",
            "error",
        )

    def _on_child_exit(self, child: _Child) -> None:
        """stdout closed: the child is gone. No auto-respawn in v1 — an agent
        that is not running should look offline, not flap (contract §5)."""
        agent_id = child.agent_id
        with self._lock:
            if self._pending.get(agent_id) is child:
                self._pending.pop(agent_id, None)
                timer, child.timer = child.timer, None
                is_pending = True
            elif self._children.get(agent_id) is child:
                self._children.pop(agent_id, None)
                timer, child.timer = child.timer, None
                is_pending = False
            else:
                return  # already stopped / replaced / promoted deliberately
        if timer is not None:
            timer.cancel()
        code = child.proc.poll()
        if is_pending:
            # A revision child that died before registering: drop the staging dir,
            # leave the running (old) version untouched.
            self._discard_staging(child.dir)
            logger.warning("byo %s: revision child exited before registering (code=%s)",
                           agent_id, code)
            return
        logger.warning("byo agent %s exited (code=%s)", agent_id, code)
        if child.registered:
            self._notify(f"Your agent “{agent_id}” stopped running.", "warning")

    def _kill(self, child: _Child) -> None:
        if child.timer is not None:
            child.timer.cancel()
            child.timer = None
        if child.supervised is None:
            logger.error("BYO child %s escaped the process supervisor", child.agent_id)
            return
        try:
            child.supervised.terminate(reason=TerminationReason.QUIT)
        except Exception:  # noqa: BLE001 — already dead / no such process
            logger.debug("supervised termination failed for %s", child.agent_id,
                         exc_info=True)
