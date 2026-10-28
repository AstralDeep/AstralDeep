"""Binds the fixed sandbox pilot to reviewed in-process dispatch and normal dynamic LETS admission.
Opaque owner sessions hold bounded private artifacts and claim each ordinary permit at its actuator.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field
import hashlib
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid

from astralplane.authority import AuthorityPopulation

from orchestrator import sandbox_qualification as sq
from orchestrator.dynamic_runtime_authority import (
    DynamicRuntimeAuthority,
    dynamic_runtime_environment,
    prepare_dynamic_runtime_roots,
    remove_dynamic_runtime_roots,
)
from orchestrator.generated_lets_executor import load_protected_executor
from orchestrator.governed_dispatch import DispatchRuntime
from shared.feature_flags import flags
from shared.process_supervision import (
    ProcessSupervisor,
    TerminationReason,
    OutputStream,
)

AGENT_ID = sq.QUALIFICATION_AGENT_ID
TOOL_ID = sq.TOOL_ID
PUBLIC_MODES = ("prepare", *sq.ALL_MODES, "close")
_ID = re.compile(r"[0-9a-f]{32}")
logger = logging.getLogger("SandboxQualification")


class ToolDenied(RuntimeError):
    def __init__(self, code="sandbox_qualification_denied"):
        self.code = code
        super().__init__(code)


async def _finish(task):
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


def validate_arguments(arguments):
    if type(arguments) is not dict or set(arguments) - {"mode", "qualification_id"}:
        raise ToolDenied()
    mode = arguments.get("mode")
    identity = arguments.get("qualification_id")
    if mode not in PUBLIC_MODES or (
        mode == "prepare" and "qualification_id" in arguments
    ):
        raise ToolDenied()
    if mode != "prepare" and (
        type(identity) is not str or _ID.fullmatch(identity) is None
    ):
        raise ToolDenied()
    return dict(arguments)


def _wire_arguments(arguments):
    if type(arguments) is not dict:
        raise ToolDenied()
    public = {
        key: value for key, value in arguments.items() if key != "_delegation_token"
    }
    token = arguments.get("_delegation_token")
    if token is not None and (type(token) is not str or not 1 <= len(token) <= 65536):
        raise ToolDenied()
    return validate_arguments(public), copy.deepcopy(arguments)


def _source():
    root = Path(__file__).parent.parent
    paths = (
        Path(__file__),
        root / "agents/sandbox_qualification/sandbox_qualification_agent.py",
        root / "agents/sandbox_qualification/mcp_server.py",
        Path(sq.__file__),
        root / "shared/base_agent.py",
        root.parent / "tooling/sandbox-qualification/manifest.json",
        root.parent / "tooling/sandbox-qualification/package.json",
        root.parent / "tooling/sandbox-qualification/package-lock.json",
    )
    return tuple(hashlib.sha256(path.read_bytes()).hexdigest() for path in paths)


def trusted_service(orch):
    from agents.sandbox_qualification.mcp_server import (
        MCPServer,
        TOOL_REGISTRY,
        _CANONICAL_PROCESS_REQUEST,
    )
    from agents.sandbox_qualification.sandbox_qualification_agent import (
        SandboxQualificationAgent,
        _CANONICAL_AGENT_INIT,
    )
    from orchestrator.orchestrator import Orchestrator
    from shared.base_agent import BaseA2AAgent

    agent = getattr(orch, "local_agents", {}).get(AGENT_ID)
    service = getattr(agent, "sessions", None)
    if (
        type(orch) is not Orchestrator
        or not flags.is_enabled("sandbox_qualification")
        or not flags.is_enabled("inprocess_agents")
        or type(agent) is not SandboxQualificationAgent
        or type(service) is not QualificationToolSessions
        or service.orchestrator is not orch
        or type(agent.mcp_server) is not MCPServer
        or agent.mcp_server.orchestrator is not orch
        or MCPServer.process_request is not _CANONICAL_PROCESS_REQUEST
        or SandboxQualificationAgent.__init__ is not _CANONICAL_AGENT_INIT
        or type(agent).handle_mcp_request is not BaseA2AAgent.handle_mcp_request
        or type(agent)._build_agent_card is not BaseA2AAgent._build_agent_card
        or agent.mcp_server.tools is not TOOL_REGISTRY
        or getattr(orch, "agent_cards", {}).get(AGENT_ID) is not agent.card
        or AGENT_ID in getattr(orch, "agents", {})
        or AGENT_ID in getattr(orch, "a2a_clients", {})
        or any(
            name in vars(agent)
            for name in ("handle_mcp_request", "_build_agent_card", "agent_id")
        )
        or "process_request" in vars(agent.mcp_server)
        or not service._implementation_current()
    ):
        return None
    expected = BaseA2AAgent._build_agent_card(agent).to_dict()
    actual = agent.card.to_dict()
    for card in (expected, actual):
        metadata = dict(card.get("metadata") or {})
        metadata.pop("astral_dispatch_posture", None)
        metadata.pop("protected_executor", None)
        card["metadata"] = metadata
    return service if actual == expected else None


@dataclass(frozen=True, repr=False)
class DispatchContext:
    service: object
    request: object
    future: object
    websocket: object
    claims: object
    owner: str
    chat: str
    session: str
    source: tuple
    approval: object
    arguments: dict
    wire_arguments: dict

    def current(self):
        from orchestrator import hitl_confirmation as hitl

        orch = self.service.orchestrator
        claims = getattr(orch, "ui_sessions", {}).get(self.websocket)
        call = self.approval.call
        return (
            self.future is not None
            and trusted_service(orch) is self.service
            and self.source == _source()
            and claims is self.claims
            and claims.get("sub") == self.owner
            and claims.get("sid") == self.session
            and claims.get("exp", 0) > time.time()
            and self.approval.orchestrator is orch
            and self.approval.consumed
            and self.approval.effect_started
            and call.owner == self.owner
            and call.chat == self.chat
            and call.agent == AGENT_ID
            and call.tool == TOOL_ID
            and "source_bound" in call.risks
            and call.approval_source is None
            and call.grant_id is None
            and orch.tool_permissions.is_tool_allowed(self.owner, AGENT_ID, TOOL_ID)
            and call.arguments == hitl._arguments(self.arguments)
            and hitl._current_session(orch, self.websocket, call)
            and getattr(orch, "pending_requests", {}).get(self.request.request_id)
            is self.future
            and not self.future.done()
            and getattr(orch, "_pending_request_agent", {}).get(self.request.request_id)
            == AGENT_ID
        )


def bind_dispatch(orch, request, arguments, websocket):
    from orchestrator import hitl_confirmation as hitl

    service = trusted_service(orch)
    approval = hitl._APPROVAL.get()
    claims = getattr(orch, "ui_sessions", {}).get(websocket)
    if (
        service is None
        or type(approval) is not hitl.Approval
        or type(claims) is not dict
        or not claims.get("sub")
        or not claims.get("sid")
        or request.params.get("name") != TOOL_ID
    ):
        raise ToolDenied()
    public, wire = _wire_arguments(arguments)
    context = DispatchContext(
        service,
        request,
        orch.pending_requests.get(request.request_id),
        websocket,
        claims,
        claims["sub"],
        approval.call.chat,
        claims["sid"],
        _source(),
        approval,
        public,
        wire,
    )
    if not context.current():
        raise ToolDenied()
    request._sandbox_qualification_context = context


@dataclass
class Session:
    identity: str
    owner: str
    chat: str
    session: str
    source: object
    runtime_id: str
    expires: float
    root: Path | None = None
    authority: object = None
    executor: object = None
    runtime: object = None
    prepared_runtime: object = None
    assets: tuple = ()
    binding: object = None
    supervisor: ProcessSupervisor = field(default_factory=ProcessSupervisor)
    modes: set = field(default_factory=set)
    busy: bool = False
    closed: bool = False
    settled: bool = False
    expiry_task: object = None
    worker: object = None
    cleanup_task: object = None
    admission_finished: bool = False
    launch_lock: object = field(default_factory=threading.Lock)


class QualificationToolSessions:
    MAX_ACTIVE = 2
    MAX_TOTAL = 4
    LIFETIME = 300

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self._states = {}
        self._lock = asyncio.Lock()
        self._reconcile_lock = asyncio.Lock()
        self._prepares = set()
        self._closing = False
        self._epoch = 0
        self._shutdown_task = None
        self._source_identity = _source()

    def _implementation_current(self):
        return self._source_identity == _source() and all(
            name not in vars(self) and getattr(type(self), name) is function
            for name, function in _CANONICAL_SESSION_METHODS.items()
        )

    def _private_root(self):
        return Path(tempfile.gettempdir())

    def _composition(self):
        composition = getattr(self.orchestrator, "lets_runtime", None)
        config = getattr(composition, "config", None)
        if (
            not getattr(composition, "ready", False)
            or getattr(config, "mode", None) != "enforce"
            or getattr(composition, "lifecycle", None) is None
        ):
            raise ToolDenied("sandbox_qualification_authority_unavailable")
        return composition

    def _executor_for(self, state, binding):
        config = self._composition().config
        state.authority = DynamicRuntimeAuthority.from_binding(
            binding,
            owner_id=state.owner,
            agent_id=AGENT_ID,
            runtime_id=state.runtime_id,
            executor_instance_id=config.executor_instance_id,
        )
        roots = prepare_dynamic_runtime_roots(config, state.runtime_id)
        environment = dynamic_runtime_environment(
            os.environ,
            state.authority,
            warden_id=config.trust_manifest.warden_id,
            database_root=roots[0],
            authority_root=roots[1],
        )
        executor = load_protected_executor(environment, agent_id=AGENT_ID)
        if (
            not executor.requires_permit
            or executor.verifier is None
            or executor.host is None
        ):
            executor.close()
            raise ToolDenied("sandbox_qualification_authority_unavailable")
        return executor

    async def prepare(self, context):
        epoch = self._epoch
        task = asyncio.current_task()
        async with self._lock:
            self._assert_open(epoch)
            if not context.current() or not self._implementation_current():
                raise ToolDenied()
            if len(self._prepares) >= self.MAX_ACTIVE:
                raise ToolDenied("sandbox_qualification_capacity")
            self._prepares.add(task)
        try:
            async with self._reconcile_lock:
                for state in tuple(self._states.values()):
                    self._assert_open(epoch)
                    if not state.settled and state.closed:
                        await self._close(state)
            self._assert_open(epoch)
            return await self._prepare(context, epoch)
        finally:
            self._prepares.discard(task)

    def _assert_open(self, epoch):
        if self._closing or epoch != self._epoch:
            raise ToolDenied()

    def _assert_current(self, state, context=None):
        current = (
            context.current() if context is not None else self._implementation_current()
        )
        if (
            not current
            or self._closing
            or state.closed
            or state.expires <= time.monotonic()
        ):
            raise ToolDenied()

    def _assert_preparing(self, state, context, epoch):
        self._assert_open(epoch)
        self._assert_current(state, context)

    async def _prepare(self, context, epoch):
        composition = self._composition()
        async with self._lock:
            self._assert_open(epoch)
            active = [state for state in self._states.values() if not state.settled]
            if len(active) >= self.MAX_ACTIVE or any(
                state.owner == context.owner for state in active
            ):
                raise ToolDenied("sandbox_qualification_capacity")
            while len(self._states) >= self.MAX_TOTAL:
                old = next(
                    (key for key, state in self._states.items() if state.settled), None
                )
                if old is None:
                    raise ToolDenied("sandbox_qualification_capacity")
                self._states.pop(old)
            identity = uuid.uuid4().hex
            state = Session(
                identity,
                context.owner,
                context.chat,
                context.session,
                context.source,
                str(uuid.uuid4()),
                time.monotonic() + self.LIFETIME,
            )
            self._states[identity] = state
        try:
            state.root = Path(
                tempfile.mkdtemp(prefix="astral-srt-tool-", dir=self._private_root())
            )
            admission_task = asyncio.create_task(
                composition.lifecycle.admit_new_runtime(
                    owner_id=state.owner,
                    agent_id=AGENT_ID,
                    runtime_id=state.runtime_id,
                    population=AuthorityPopulation.SERVER_DYNAMIC,
                    declared_scopes=(sq.TOOL_SCOPE,),
                    executor_conformant=True,
                )
            )
            try:
                result = await asyncio.shield(admission_task)
            except BaseException:
                try:
                    result = await _finish(admission_task)
                    state.binding = result.binding
                    state.admission_finished = True
                except BaseException:
                    pass
                raise
            state.binding = result.binding
            state.admission_finished = True
            if (
                not result.protected
                or result.error_code
                or state.binding is None
                or not context.current()
            ):
                raise ToolDenied("sandbox_qualification_admission_denied")
            self._assert_preparing(state, context, epoch)
            self._check_binding(state, state.binding)
            executor_task = asyncio.create_task(
                asyncio.to_thread(self._executor_for, state, state.binding)
            )
            try:
                state.executor = await asyncio.shield(executor_task)
            except BaseException:
                state.executor = await _finish(executor_task)
                raise
            self._assert_preparing(state, context, epoch)
            runtime_task = asyncio.create_task(
                asyncio.to_thread(self._prepare_runtime, state)
            )
            try:
                await asyncio.shield(runtime_task)
            except BaseException:
                await _finish(runtime_task)
                raise
            self._assert_preparing(state, context, epoch)
            await self._current_binding(state)
            self._assert_preparing(state, context, epoch)
            state.expiry_task = asyncio.create_task(self._expire(state))
            return {
                "status": "ready",
                "qualification_id": identity,
                "runtime_assets_sha256": hashlib.sha256(
                    repr(state.assets).encode()
                ).hexdigest(),
            }
        except BaseException:
            await _finish(asyncio.create_task(self._close(state)))
            if state.settled:
                self._states.pop(identity, None)
            raise

    def _check_binding(self, state, binding):
        if (
            binding is None
            or binding.owner_id != state.owner
            or binding.agent_id != AGENT_ID
            or binding.runtime_id != state.runtime_id
            or binding.state.value != "active"
            or binding.population.value != "server_dynamic"
            or binding.lease_expires_at_ns <= time.time_ns()
            or (
                state.binding is not None
                and any(
                    getattr(binding, name) != getattr(state.binding, name)
                    for name in (
                        "binding_id",
                        "lease_id",
                        "lineage_id",
                        "runtime_generation",
                    )
                )
            )
        ):
            raise ToolDenied("sandbox_qualification_binding_changed")

    async def _current_binding(self, state):
        binding = await asyncio.to_thread(
            self._composition().lifecycle.latest_binding,
            owner_id=state.owner,
            agent_id=AGENT_ID,
            population=AuthorityPopulation.SERVER_DYNAMIC,
        )
        self._check_binding(state, binding)
        return binding

    async def resolve(self, context, identity):
        state = self._states.get(identity)
        if (
            state is None
            or state.closed
            or state.expires <= time.monotonic()
            or not context.current()
            or (state.owner, state.chat, state.session, state.source)
            != (context.owner, context.chat, context.session, context.source)
        ):
            raise ToolDenied()
        await self._current_binding(state)
        self._assert_current(state, context)
        return state

    async def runtime_for_owner(self, owner):
        state = next(
            (
                state
                for state in self._states.values()
                if state.owner == owner and not state.closed
            ),
            None,
        )
        if state is None:
            return None
        self._assert_current(state)
        binding = await self._current_binding(state)
        try:
            self._assert_current(state)
            await asyncio.to_thread(self._verify_runtime, state)
            self._assert_current(state)
            binding = await self._current_binding(state)
            self._assert_current(state)
        except BaseException:
            await self._close(state)
            raise
        authority = DynamicRuntimeAuthority.from_binding(
            binding,
            owner_id=owner,
            agent_id=AGENT_ID,
            runtime_id=state.runtime_id,
            executor_instance_id=self._composition().config.executor_instance_id,
        )
        return DispatchRuntime(
            owner,
            AGENT_ID,
            "server_dynamic",
            state.runtime_id,
            binding.runtime_generation,
            authority.executor_audience,
            True,
            "protected_executor",
        )

    async def execute(self, context, arguments, metadata):
        if (
            type(context) is not DispatchContext
            or context.service is not self
            or not context.current()
        ):
            raise ToolDenied()
        public, arguments = _wire_arguments(arguments)
        if public != context.arguments or arguments != context.wire_arguments:
            raise ToolDenied()
        mode = arguments["mode"]
        if mode == "prepare":
            return await self.prepare(context)
        state = await self.resolve(context, arguments["qualification_id"])
        async with self._lock:
            if (
                state.busy
                or mode in state.modes
                or (
                    mode not in {sq.AUTHORIZED_MODE, "close"}
                    and sq.AUTHORIZED_MODE not in state.modes
                )
            ):
                raise ToolDenied()
            state.busy = True
            state.modes.add(mode)
        try:
            if mode == "close":
                self._assert_current(state, context)
                state.executor.verify_and_claim(
                    metadata=metadata,
                    final_arguments=arguments,
                    tool_id=TOOL_ID,
                    tool_scope=sq.TOOL_SCOPE,
                )
                await self._close(state)
                return {
                    "status": "closed" if state.settled else "closing",
                    "qualification_id": state.identity,
                }
            task = asyncio.create_task(
                asyncio.to_thread(self._effect, state, context, arguments, metadata)
            )
            state.worker = task
            try:
                while not task.done():
                    await asyncio.sleep(0.25)
                    await self.resolve(context, state.identity)
                result = await task
                if result["status"] != sq.STATUS_PROBE:
                    await self._close(state)
                return result
            except BaseException:
                state.closed = True
                await asyncio.to_thread(self._stop_launches, state)
                raise
            finally:
                self._terminate(state)
                await _finish(task)
        except BaseException:
            await self._close(state)
            raise
        finally:
            state.busy = False
            state.worker = None

    def _effect(self, state, context, arguments, metadata):
        mode = arguments["mode"]
        runtime = state.runtime
        session = sq.prepare_session(
            session_dir=state.root / mode / "control",
            scratch_root=state.root / mode / "scratch",
            qualification_id=state.identity,
        )
        settings = sq.build_srt_settings(session, runtime)
        sq.validate_srt_settings(settings, session=session, runtime=runtime)
        profile = sq.write_immutable_profile(session, settings)
        tcp, unix = None, None
        checks = []
        try:
            if mode == "network_connect":
                tcp = sq.ProbeTcpListener()
            if mode == "unix_ipc":
                unix = sq.ProbeUnixListener(sq._ipc_listener_path(session))
            spec = sq.build_effect_spec(
                session,
                mode=mode,
                connect_port=tcp.port if tcp else 0,
                ipc_path=str(sq._ipc_listener_path(session)),
            )
            sq.write_effect_spec(session, spec)
            with state.launch_lock:
                self._assert_current(state, context)
                self._verify_runtime(state)
                self._assert_current(state, context)
                current = self._composition().lifecycle.latest_binding(
                    owner_id=state.owner,
                    agent_id=AGENT_ID,
                    population=AuthorityPopulation.SERVER_DYNAMIC,
                )
                self._check_binding(state, current)
                self._assert_current(state, context)
                state.executor.verify_and_claim(
                    metadata=metadata,
                    final_arguments=arguments,
                    tool_id=TOOL_ID,
                    tool_scope=sq.TOOL_SCOPE,
                )
                self._assert_current(state, context)
                process = sq.launch_sandboxed_effect(
                    state.supervisor, session, runtime, session.profile_path
                )
            descendant = None
            if mode == "spawn_descendant":
                descendant = sq.capture_descendant_identity(
                    process, session, timeout=30
                )
                checks.append(
                    sq._check(
                        "descendant_pid_namespace",
                        True,
                        sq._descendant_identity_label(descendant),
                    )
                )
            elif mode == "hang":
                process.wait_for_line(
                    OutputStream.STDOUT, prefix=sq.EFFECT_READY_PREFIX, timeout=30
                )
                checks.append(sq._check("effect_ready", True, mode))
            snapshot, reason, timed_out = sq.await_effect(
                process,
                deadline_seconds=2 if mode in {"hang", "spawn_descendant"} else 30,
            )
            checks.extend(sq.launch_outcome_checks(snapshot))
            checks.extend(sq.tree_cleanup_checks(snapshot))
            checks.extend(sq.degradation_checks(process))
            if mode in {sq.AUTHORIZED_MODE, "exhaust_output"}:
                checks.extend(
                    sq.authorized_effect_checks(session, snapshot, marker_mode=mode)
                )
            if mode == "exhaust_output":
                checks.extend(sq.output_bound_checks(snapshot, expect_dropped=True))
            elif mode == "read_secret":
                checks.extend(sq.secret_read_checks(session, process))
            elif mode == "write_outside":
                checks.extend(sq.outside_write_checks(session, spec))
            elif mode == "network_connect":
                checks.extend(sq.network_checks(tcp, sq.read_marker(session, mode)))
            elif mode == "unix_ipc":
                checks.extend(
                    sq.ipc_checks(session, unix, sq.read_marker(session, mode))
                )
            elif mode == "symlink_escape":
                checks.extend(sq.symlink_checks(session))
            elif mode in {"hang", "spawn_descendant"}:
                checks.append(
                    sq._check(
                        "deadline_enforcement",
                        timed_out,
                        "deadline" if timed_out else "hang_not_cancelled",
                    )
                )
                if mode == "spawn_descendant":
                    checks.append(sq.descendant_cleanup_check(descendant))
            failures = [
                f"{check.name}:{check.detail}" for check in checks if not check.held
            ]
            return {
                "status": sq.STATUS_FAILED if failures else sq.STATUS_PROBE,
                "qualification_id": state.identity,
                "mode": mode,
                "failure_codes": failures,
                "checks": [vars(check) for check in checks],
                "profile_sha256": hashlib.sha256(profile).hexdigest(),
                "artifact_sha512": runtime.artifact_sha512,
                "runtime_assets_sha256": hashlib.sha256(
                    repr(state.assets).encode()
                ).hexdigest(),
                "receipt_sha256": sq.receipt_digest(metadata),
            }
        finally:
            self._terminate(state)
            if tcp is not None:
                tcp.close()
            if unix is not None:
                unix.close()

    def _terminate(self, state):
        for snapshot in state.supervisor.snapshots():
            state.supervisor.terminate(
                snapshot.process_id, reason=TerminationReason.CANCEL
            )

    def _runtime_cache(self, state):
        cache = state.root / "artifacts"
        if cache.exists():
            return cache
        cache.mkdir(mode=0o700)
        pin = sq.default_artifact_pin()
        _package, _lock, dependencies = sq._locked_installation(pin)
        source = Path(tempfile.gettempdir()) / f"astral-srt-qualification-{pin.version}"
        for _url, digest in dependencies:
            name = (
                f"sandbox-runtime-{pin.version}.tgz"
                if digest == pin.sha512_hex
                else f"{digest}.tgz"
            )
            with os.fdopen(
                os.open(source / name, os.O_RDONLY | os.O_NOFOLLOW), "rb"
            ) as stream:
                metadata = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or not 0 < metadata.st_size <= 10 * 1024 * 1024
                ):
                    raise ToolDenied("sandbox_qualification_cache_invalid")
                payload = stream.read(10 * 1024 * 1024 + 1)
            if hashlib.sha512(payload).hexdigest() != digest:
                raise ToolDenied("sandbox_qualification_cache_invalid")
            sq._write_exclusive_artifact(cache / name, payload, mode=0o400)
        return cache

    def _prepare_runtime(self, state):
        cache = self._runtime_cache(state)
        state.runtime = sq.prepare_srt_runtime(cache_dir=cache)
        support = sq.probe_support(runtime=state.runtime)
        if not support.available:
            raise ToolDenied("sandbox_qualification_host_unavailable")
        state.prepared_runtime = state.runtime
        state.assets = self._asset_identity(state.runtime)

    def _asset_identity(self, runtime):
        if type(runtime) is not sq.SrtRuntime:
            raise ToolDenied("sandbox_qualification_runtime_changed")
        paths = [
            runtime.node_path,
            runtime.apply_seccomp_path,
            runtime.bwrap_path,
            runtime.socat_path,
        ]
        for name in ("python3", "rg", "bash", "sh"):
            command = shutil.which(name)
            if command is None:
                raise ToolDenied("sandbox_qualification_runtime_changed")
            paths.append(Path(command))
        installation = runtime.artifact_root.parents[1]
        paths.extend([installation, *installation.rglob("*")])
        identities, seen, total_bytes = [], set(), 0
        while paths:
            path = paths.pop()
            name = str(path)
            if name in seen:
                continue
            seen.add(name)
            if len(seen) > 4096:
                raise ToolDenied("sandbox_qualification_runtime_changed")
            info = path.lstat()
            common = (name, info.st_dev, info.st_ino, info.st_mode, info.st_size)
            if stat.S_ISLNK(info.st_mode):
                identities.append((*common, os.readlink(path)))
                paths.append(path.resolve(strict=True))
            elif stat.S_ISREG(info.st_mode):
                total_bytes += info.st_size
                if total_bytes > 512 * 1024 * 1024:
                    raise ToolDenied("sandbox_qualification_runtime_changed")
                digest = hashlib.sha256()
                with os.fdopen(
                    os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb"
                ) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                identities.append((*common, digest.hexdigest()))
            elif stat.S_ISDIR(info.st_mode):
                identities.append((*common, "directory"))
            else:
                raise ToolDenied("sandbox_qualification_runtime_changed")
        return tuple(sorted(identities))

    def _verify_runtime(self, state):
        if (
            state.runtime is not state.prepared_runtime
            or not state.assets
            or self._asset_identity(state.runtime) != state.assets
        ):
            raise ToolDenied("sandbox_qualification_runtime_changed")

    async def _expire(self, state):
        await asyncio.sleep(max(0, state.expires - time.monotonic()))
        await self._close(state)

    async def _close(self, state):
        state.closed = True
        expiry = state.expiry_task
        state.expiry_task = None
        if expiry is not None and expiry is not asyncio.current_task():
            expiry.cancel()
        task = state.cleanup_task
        if task is None or task.done():
            task = asyncio.create_task(self._cleanup(state))
            state.cleanup_task = task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await _finish(task)
            raise
        finally:
            if expiry is not None and expiry is not asyncio.current_task():
                await _finish(asyncio.gather(expiry, return_exceptions=True))

    async def _cleanup(self, state):
        await asyncio.to_thread(self._stop_launches, state)
        if state.worker is not None and state.worker is not asyncio.current_task():
            await asyncio.gather(asyncio.shield(state.worker), return_exceptions=True)
        if state.executor is not None:
            await asyncio.to_thread(state.executor.close)
            state.executor = None
        try:
            if state.binding is None:
                latest = await asyncio.to_thread(
                    self._composition().lifecycle.latest_binding,
                    owner_id=state.owner,
                    agent_id=AGENT_ID,
                    population=AuthorityPopulation.SERVER_DYNAMIC,
                )
                if latest is not None and (
                    latest.owner_id == state.owner
                    and latest.agent_id == AGENT_ID
                    and latest.runtime_id == state.runtime_id
                    and latest.population.value == "server_dynamic"
                ):
                    state.binding = latest
                elif latest is None and state.admission_finished:
                    state.settled = True
            if state.binding is not None:
                result = await self._composition().lifecycle.close_runtime_generation(
                    owner_id=state.owner,
                    agent_id=AGENT_ID,
                    runtime_id=state.runtime_id,
                    runtime_generation=state.binding.runtime_generation,
                    population=AuthorityPopulation.SERVER_DYNAMIC,
                )
                terminal = result.binding
                if (
                    terminal is not None
                    and terminal.state.terminal
                    and terminal.population.value == "server_dynamic"
                    and all(
                        getattr(terminal, name) == getattr(state.binding, name)
                        for name in (
                            "owner_id", "agent_id", "runtime_id", "runtime_generation",
                            "binding_id", "lease_id", "lineage_id",
                        )
                    )
                ):
                    state.settled = True
                    remove_dynamic_runtime_roots(
                        self._composition().config, state.runtime_id
                    )
        except Exception:
            state.settled = False
        if not state.settled:
            logger.warning("sandbox_qualification_authority_close_pending")
        if state.root is not None:
            shutil.rmtree(state.root, ignore_errors=True)
            state.root = None

    def _stop_launches(self, state):
        with state.launch_lock:
            self._terminate(state)

    async def shutdown(self):
        if self._shutdown_task is None:
            self._closing = True
            self._epoch += 1
            self._shutdown_task = asyncio.create_task(self._shutdown())
        try:
            await asyncio.shield(self._shutdown_task)
        except asyncio.CancelledError:
            await _finish(self._shutdown_task)
            raise

    async def _shutdown(self):
        prepares = tuple(self._prepares)
        for task in prepares:
            task.cancel()
        if prepares:
            await asyncio.gather(*prepares, return_exceptions=True)
        for state in tuple(self._states.values()):
            await self._close(state)


_CANONICAL_SESSION_METHODS = {
    name: getattr(QualificationToolSessions, name)
    for name in (
        "prepare",
        "_prepare",
        "_assert_open",
        "_assert_current",
        "_assert_preparing",
        "execute",
        "_effect",
        "runtime_for_owner",
        "_executor_for",
        "_current_binding",
        "resolve",
        "_composition",
        "_check_binding",
        "_runtime_cache",
        "_prepare_runtime",
        "_verify_runtime",
        "_asset_identity",
        "_close",
        "_cleanup",
        "shutdown",
        "_shutdown",
    )
}
