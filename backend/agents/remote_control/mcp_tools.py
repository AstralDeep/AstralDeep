#!/usr/bin/env python3
"""The 9 mutating remote-compute verbs (directory/job/service/package/process/upload
control) run over orchestrator/remote_transport.py; destructive ones are pre-gated by
orchestrator/remote_confirmation.py before reaching this module.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from astralprims import Alert, Card, Table, Text

from orchestrator import remote_machines
from orchestrator.credential_manager import CredentialNotConfigured, CredentialUndecryptable
from orchestrator.remote_confirmation import DESTRUCTIVE_CLASSIFICATION
from orchestrator.remote_machines import MachineNotFound
from orchestrator.remote_transport import RemoteResult, Verdict, get_transport

_DB = None
_CREDMGR = None
_BLOBS = None

_MAX_FIELD = 256
_MAX_PATH = 4096
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\x1b]")
_ABS_PATH = re.compile(r"^/[^\x00]*$")
_INT = re.compile(r"^\d+$")
_NAME = re.compile(r"^[A-Za-z0-9._@+-]+$")

_SERVICE_ACTIONS = ("start", "stop", "restart", "enable", "disable")
_PACKAGE_ACTIONS = ("install", "remove")
_SIGNALS = ("TERM", "KILL")


def register_deps(db, credmgr, blob_store) -> None:
    global _DB, _CREDMGR, _BLOBS
    _DB, _CREDMGR, _BLOBS = db, credmgr, blob_store


def _sanitize(value: Any, limit: int = _MAX_FIELD) -> str:
    if value is None:
        return ""
    text = _CTRL.sub("", str(value))
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


def _ui(components: List[Any], data: Optional[Dict] = None) -> Dict[str, Any]:
    return {"_ui_components": [c.to_dict() for c in components], "_data": data}


def _ok(title: str, components: List[Any], data: Optional[Dict] = None) -> Dict[str, Any]:
    return {"_ui_components": [Card(title=title, content=components).to_dict()], "_data": data}


def _fail(verdict: Any, machine: str, next_action: str = "") -> Dict[str, Any]:
    v = verdict.value if isinstance(verdict, Verdict) else str(verdict)
    text = f"{_sanitize(machine, 80)}: {v}" + (f" — {next_action}" if next_action else "")
    return _ui([Alert(message=text, variant="error")],
               data={"verdict": v, "machine": machine, "next_action": next_action})


def _result_fail(res: RemoteResult) -> Dict[str, Any]:
    return _fail(res.verdict, res.machine, res.next_action)


def _stderr_tail(res: RemoteResult) -> str:
    lines = [ln for ln in (getattr(res, "stderr", "") or "").splitlines() if ln.strip()]
    return _sanitize(lines[-1], 240) if lines else ""


def _resolve(user_id: Optional[str], ref: Optional[str]):
    if not user_id:
        return None, _fail(Verdict.UNATTENDED_REFUSED, ref or "?", "sign in to use remote machines")
    if not ref:
        return None, _fail(Verdict.INVALID_ARGUMENT, "?", "name the machine (its label, address, or id)")
    row = remote_machines.resolve_machine(_DB, user_id, ref)
    if row is None:
        return None, _fail(Verdict.NOT_FOUND, ref, "this machine is not in your inventory")
    try:
        return remote_machines.build_target(_DB, _CREDMGR, user_id, row["machine_id"]), None
    except MachineNotFound:
        return None, _fail(Verdict.NOT_FOUND, ref, "this machine is not in your inventory")
    except CredentialNotConfigured:
        return None, _fail(Verdict.CREDENTIAL_NOT_CONFIGURED, row["label"], "add a credential for this machine")
    except CredentialUndecryptable:
        return None, _fail(Verdict.CREDENTIAL_UNDECRYPTABLE, row["label"], "re-enter the credential for this machine")


def _finish(res: RemoteResult, target, success_title: str, success_comps: List[Any],
            data: Dict[str, Any], fail_next: str) -> Dict[str, Any]:
    if not res.ok:
        return _result_fail(res)
    if res.exit_status is None:
        return _fail(Verdict.UNCONFIRMED, target.label,
                     "the command's output was truncated; check the machine before re-issuing")
    if res.exit_status != 0:
        tail = _stderr_tail(res)
        reason = f": {tail}" if tail else f"; {fail_next}"
        return _fail(Verdict.PARTIAL, target.label,
                     f"the machine rejected the operation (exit {res.exit_status}){reason}")
    return _ok(success_title, success_comps, {**data, "exit_status": 0})


def _bad_path(p: Any) -> bool:
    return not (isinstance(p, str) and 0 < len(p.encode("utf-8", "ignore")) <= _MAX_PATH
                and _ABS_PATH.match(p))


def _int_token(v: Any) -> Optional[str]:
    s = str(v).strip()
    return s if _INT.match(s) else None


def make_directory(**kwargs) -> Dict[str, Any]:
    user_id = kwargs.get("user_id")
    ref = kwargs.get("machine_id") or kwargs.get("machine") or kwargs.get("label")
    path = kwargs.get("path")
    target, err = _resolve(user_id, ref)
    if err:
        return err
    if _bad_path(path):
        return _fail(Verdict.INVALID_ARGUMENT, target.label, "path must be an absolute path")
    res = get_transport().run(target, ["mkdir", "-p", path], timeout=20.0, retryable=False)
    return _finish(res, target, f"Directory ready — {_sanitize(target.label, 60)}",
                   [Text(content=f"Created (or already present): {_sanitize(path, 200)}", variant="body")],
                   {"path": path}, "check the parent directory exists and is writable")


def remove_path(**kwargs) -> Dict[str, Any]:
    user_id = kwargs.get("user_id")
    ref = kwargs.get("machine_id") or kwargs.get("machine") or kwargs.get("label")
    path = kwargs.get("path")
    recursive = bool(kwargs.get("recursive", False))
    target, err = _resolve(user_id, ref)
    if err:
        return err
    if _bad_path(path):
        return _fail(Verdict.INVALID_ARGUMENT, target.label, "path must be an absolute path")
    argv = ["rm", "-r", "-f", path] if recursive else ["rm", "-f", path]
    res = get_transport().run(target, argv, timeout=30.0, retryable=False)
    return _finish(res, target, f"Deleted — {_sanitize(target.label, 60)}",
                   [Text(content=f"Removed: {_sanitize(path, 200)}"
                                 + (" (recursive)" if recursive else ""), variant="body")],
                   {"path": path, "recursive": recursive},
                   "the path may need -r (recursive) or you may lack permission")


def cancel_job(**kwargs) -> Dict[str, Any]:
    user_id = kwargs.get("user_id")
    ref = kwargs.get("machine_id") or kwargs.get("machine") or kwargs.get("label")
    job_id = _int_token(kwargs.get("job_id"))
    target, err = _resolve(user_id, ref)
    if err:
        return err
    if job_id is None:
        return _fail(Verdict.INVALID_ARGUMENT, target.label, "job_id must be a numeric Slurm job id")
    res = get_transport().run(target, ["scancel", job_id], timeout=20.0, retryable=False)
    return _finish(res, target, f"Job cancelled — {_sanitize(target.label, 60)}",
                   [Text(content=f"Requested cancellation of job {job_id}.", variant="body")],
                   {"job_id": job_id}, "the job may be yours to cancel, already gone, or invalid")


def control_service(**kwargs) -> Dict[str, Any]:
    user_id = kwargs.get("user_id")
    ref = kwargs.get("machine_id") or kwargs.get("machine") or kwargs.get("label")
    service = kwargs.get("service_name")
    action = kwargs.get("action")
    target, err = _resolve(user_id, ref)
    if err:
        return err
    if not (isinstance(service, str) and _NAME.match(service)):
        return _fail(Verdict.INVALID_ARGUMENT, target.label, "service_name has an invalid character")
    if action not in _SERVICE_ACTIONS:
        return _fail(Verdict.INVALID_ARGUMENT, target.label,
                     f"action must be one of {', '.join(_SERVICE_ACTIONS)}")
    res = get_transport().run(target, ["systemctl", action, service], timeout=30.0, retryable=False)
    return _finish(res, target, f"Service {action} — {_sanitize(target.label, 60)}",
                   [Text(content=f"{action.capitalize()} {_sanitize(service, 120)}.", variant="body")],
                   {"service_name": service, "action": action},
                   "you may lack privileges for this service, or it may not exist")


_PKG_MANAGERS = ("apt-get", "dnf", "yum", "zypper")


def _pkg_argv(manager: str, action: str, package: str) -> List[str]:
    verb = {"install": "install", "remove": "remove"}[action]
    if manager == "zypper":
        return ["zypper", "--non-interactive", verb, package]
    return [manager, verb, "-y", package]


def manage_package(**kwargs) -> Dict[str, Any]:
    user_id = kwargs.get("user_id")
    ref = kwargs.get("machine_id") or kwargs.get("machine") or kwargs.get("label")
    package = kwargs.get("package_name")
    action = kwargs.get("action")
    target, err = _resolve(user_id, ref)
    if err:
        return err
    if not (isinstance(package, str) and _NAME.match(package)):
        return _fail(Verdict.INVALID_ARGUMENT, target.label, "package_name has an invalid character")
    if action not in _PACKAGE_ACTIONS:
        return _fail(Verdict.INVALID_ARGUMENT, target.label,
                     f"action must be one of {', '.join(_PACKAGE_ACTIONS)}")
    transport = get_transport()
    probe = transport.run(target, ["which", *_PKG_MANAGERS], timeout=20.0, retryable=True)
    if not probe.ok:
        return _result_fail(probe)
    found = [m for m in _PKG_MANAGERS if m in (probe.stdout or "")]
    if not found:
        return _fail(Verdict.INVALID_ARGUMENT, target.label,
                     "no supported system package manager was found on this machine")
    manager = found[0]
    res = transport.run(target, _pkg_argv(manager, action, package), timeout=120.0, retryable=False)
    return _finish(res, target, f"Package {action} — {_sanitize(target.label, 60)}",
                   [Text(content=f"{action.capitalize()} {_sanitize(package, 120)} via {manager}.",
                         variant="body")],
                   {"package_name": package, "action": action, "manager": manager},
                   "you likely need elevated privileges to manage system packages")


def signal_process(**kwargs) -> Dict[str, Any]:
    user_id = kwargs.get("user_id")
    ref = kwargs.get("machine_id") or kwargs.get("machine") or kwargs.get("label")
    pid = _int_token(kwargs.get("pid"))
    signal = kwargs.get("signal")
    target, err = _resolve(user_id, ref)
    if err:
        return err
    if pid is None:
        return _fail(Verdict.INVALID_ARGUMENT, target.label, "pid must be numeric")
    if signal not in _SIGNALS:
        return _fail(Verdict.INVALID_ARGUMENT, target.label,
                     f"signal must be one of {', '.join(_SIGNALS)}")
    res = get_transport().run(target, ["kill", f"-{signal}", pid], timeout=15.0, retryable=False)
    return _finish(res, target, f"Signal sent — {_sanitize(target.label, 60)}",
                   [Text(content=f"Sent SIG{signal} to process {pid}.", variant="body")],
                   {"pid": pid, "signal": signal},
                   "the process may be gone or owned by another user")


def _sbatch_flags(kwargs: Dict[str, Any], label: str):
    flags: List[str] = []
    partition = kwargs.get("partition")
    if partition is not None:
        if not (isinstance(partition, str) and _NAME.match(partition)):
            return None, _fail(Verdict.INVALID_ARGUMENT, label, "partition has an invalid character")
        flags.append(f"--partition={partition}")
    time_limit = kwargs.get("time_limit")
    if time_limit is not None:
        if not (isinstance(time_limit, str) and re.match(r"^[0-9:\-]+$", time_limit)):
            return None, _fail(Verdict.INVALID_ARGUMENT, label, "time_limit must be a Slurm time string")
        flags.append(f"--time={time_limit}")
    nodes = kwargs.get("nodes")
    if nodes is not None:
        try:
            n = int(nodes)
        except (TypeError, ValueError):
            n = 0
        if n < 1:
            return None, _fail(Verdict.INVALID_ARGUMENT, label, "nodes must be an integer ≥ 1")
        flags.append(f"--nodes={n}")
    gpus = kwargs.get("gpus")
    if gpus is not None:
        try:
            g = int(gpus)
        except (TypeError, ValueError):
            g = -1
        if g < 0:
            return None, _fail(Verdict.INVALID_ARGUMENT, label, "gpus must be an integer ≥ 0")
        if g > 0:
            flags.append(f"--gpus={g}")
    job_name = kwargs.get("job_name")
    if job_name is not None:
        if not (isinstance(job_name, str) and _NAME.match(job_name)):
            return None, _fail(Verdict.INVALID_ARGUMENT, label, "job_name has an invalid character")
        flags.append(f"--job-name={job_name}")
    account = kwargs.get("account")
    if account is not None:
        if not (isinstance(account, str) and _NAME.match(account)):
            return None, _fail(Verdict.INVALID_ARGUMENT, label, "account has an invalid character")
        flags.append(f"--account={account}")
    return flags, None


def submit_job(**kwargs) -> Dict[str, Any]:
    import uuid as _uuid

    user_id = kwargs.get("user_id")
    chat_id = kwargs.get("session_id")
    ref = kwargs.get("machine_id") or kwargs.get("machine") or kwargs.get("label")
    script_path = kwargs.get("script_path")
    job_name = kwargs.get("job_name")
    notify = kwargs.get("notify_on_finish")
    notify = True if notify is None else bool(notify)
    target, err = _resolve(user_id, ref)
    if err:
        return err
    if _bad_path(script_path):
        return _fail(Verdict.INVALID_ARGUMENT, target.label, "script_path must be an absolute path")
    flags, ferr = _sbatch_flags(kwargs, target.label)
    if ferr:
        return ferr
    nonce = _uuid.uuid4().hex[:12]
    argv = ["sbatch", "--parsable", f"--comment=astral:{nonce}", *flags, script_path]
    res = get_transport().run(target, argv, timeout=30.0, retryable=False)
    if not res.ok:
        return _result_fail(res)
    if res.exit_status not in (0, None) or not (res.stdout or "").strip():
        tail = _stderr_tail(res)
        return _fail(Verdict.PARTIAL, target.label,
                     f"sbatch did not accept the job: {tail}" if tail
                     else "sbatch did not accept the job; check the script path, partition, and account")
    job_id = _sanitize((res.stdout or "").strip().split(";")[0].split()[0], 24)
    if not job_id:
        return _fail(Verdict.UNCONFIRMED, target.label,
                     "the job may have been submitted but no id came back; check the queue")

    component_id = f"au_rjob_{job_id}"
    from orchestrator import remote_jobs
    try:
        remote_jobs.create_tracked_job(
            _DB, owner_user_id=user_id, machine_id=target.machine_id, chat_id=chat_id,
            scheduler_job_id=job_id, submit_marker=nonce, output_path=None,
            component_id=component_id, job_name=job_name or "", notify_on_finish=notify)
    except Exception:  # noqa: BLE001
        pass
    card = remote_jobs.render_job_card(
        job_id=job_id, machine_label=target.label, state="submitted", terminal=False,
        component_id=component_id, job_name=job_name or None)
    return {"_ui_components": [card],
            "_data": {"job_id": job_id, "state": "submitted", "tracked": True,
                      "script_path": script_path, "notify_on_finish": notify}}


_MAX_SCRIPT_BYTES = 64 * 1024


def run_job(**kwargs) -> Dict[str, Any]:
    import uuid as _uuid

    user_id = kwargs.get("user_id")
    chat_id = kwargs.get("session_id")
    ref = kwargs.get("machine_id") or kwargs.get("machine") or kwargs.get("label")
    script = kwargs.get("script")
    job_name = kwargs.get("job_name")
    notify = kwargs.get("notify_on_finish")
    notify = True if notify is None else bool(notify)
    target, err = _resolve(user_id, ref)
    if err:
        return err
    if not (isinstance(script, str) and script.strip()):
        return _fail(Verdict.INVALID_ARGUMENT, target.label, "provide the job's script text")
    if len(script.encode("utf-8", "ignore")) > _MAX_SCRIPT_BYTES:
        return _fail(Verdict.INVALID_ARGUMENT, target.label, "script is too large (max 64 KB)")
    flags, ferr = _sbatch_flags(kwargs, target.label)
    if ferr:
        return ferr

    tx = get_transport()
    pwd = tx.run(target, ["pwd"], timeout=15.0, retryable=True)
    if not pwd.ok:
        return _result_fail(pwd)
    home = (pwd.stdout or "").strip().splitlines()[0] if (pwd.stdout or "").strip() else ""
    base = f"{home}/.astral_jobs" if home.startswith("/") else ".astral_jobs"
    nonce = _uuid.uuid4().hex[:12]
    script_path = f"{base}/astral-{nonce}.sbatch"
    output_path = f"{base}/astral-{nonce}.out"

    mk = tx.run(target, ["mkdir", "-p", base], timeout=15.0, retryable=False)
    if not mk.ok:
        return _result_fail(mk)
    body = "#!/bin/bash\n" + script.replace("\r\n", "\n").replace("\r", "\n")
    if not body.endswith("\n"):
        body += "\n"
    put = tx.put_file(target, body.encode("utf-8"), script_path, timeout=30.0)
    if not put.ok:
        return _result_fail(put)

    argv = ["sbatch", "--parsable", f"--output={output_path}",
            f"--comment=astral:{nonce}", *flags, script_path]
    res = tx.run(target, argv, timeout=30.0, retryable=False)
    if not res.ok:
        return _result_fail(res)
    if res.exit_status not in (0, None) or not (res.stdout or "").strip():
        tail = _stderr_tail(res)
        return _fail(Verdict.PARTIAL, target.label,
                     f"sbatch did not accept the job: {tail}" if tail
                     else "sbatch did not accept the job; check partition/account and the script")
    job_id = _sanitize((res.stdout or "").strip().split(";")[0].split()[0], 24)
    if not job_id:
        return _fail(Verdict.UNCONFIRMED, target.label,
                     "the job may have been submitted but no id came back; check the queue")

    component_id = f"au_rjob_{job_id}"
    from orchestrator import remote_jobs
    try:
        remote_jobs.create_tracked_job(
            _DB, owner_user_id=user_id, machine_id=target.machine_id, chat_id=chat_id,
            scheduler_job_id=job_id, submit_marker=nonce, output_path=output_path,
            component_id=component_id, job_name=job_name or "", notify_on_finish=notify)
    # Job already submitted — a tracking-row failure isn't fatal
    except Exception:  # noqa: BLE001
        pass
    card = remote_jobs.render_job_card(
        job_id=job_id, machine_label=target.label, state="submitted", terminal=False,
        component_id=component_id, job_name=job_name or None)
    return {"_ui_components": [card],
            "_data": {"job_id": job_id, "state": "submitted", "tracked": True,
                      "output_path": output_path, "notify_on_finish": notify}}


def upload_file(**kwargs) -> Dict[str, Any]:
    user_id = kwargs.get("user_id")
    ref = kwargs.get("machine_id") or kwargs.get("machine") or kwargs.get("label")
    attachment_id = kwargs.get("attachment_id")
    remote_path = kwargs.get("remote_path")
    target, err = _resolve(user_id, ref)
    if err:
        return err
    if _bad_path(remote_path):
        return _fail(Verdict.INVALID_ARGUMENT, target.label, "remote_path must be an absolute path")
    if not (isinstance(attachment_id, str) and attachment_id):
        return _fail(Verdict.INVALID_ARGUMENT, target.label, "attachment_id is required")
    from astralplane.errors import PlaneError

    from orchestrator.attachments.blob_access import (
        AttachmentBlobReferenceError,
        open_attachment_reader,
    )
    from orchestrator.attachments.repository import AttachmentRepository
    att = AttachmentRepository.from_plane_source(_DB).get_by_id(
        attachment_id,
        user_id,
    )
    if att is None:
        return _fail(Verdict.NOT_FOUND, target.label, "that attachment is not in your files")
    filename = getattr(att, "filename", None)
    size = int(getattr(att, "size_bytes", 0) or 0)
    if size > _MAX_UPLOAD_BYTES:
        return _fail(Verdict.INVALID_ARGUMENT, target.label, "that attachment is too large to upload")
    try:
        with open_attachment_reader(_BLOBS, att, user_id) as reader:
            data = b"".join(reader.iter_chunks())
    except (AttachmentBlobReferenceError, PlaneError):
        return _fail(Verdict.NOT_FOUND, target.label, "the attachment's stored file is missing")
    res = get_transport().put_file(target, data, remote_path, timeout=60.0)
    if not res.ok:
        return _result_fail(res)
    return _ok(f"Uploaded — {_sanitize(target.label, 60)}",
               [Table(headers=["Field", "Value"],
                      rows=[["File", _sanitize(filename or attachment_id, 120)],
                            ["Destination", _sanitize(remote_path, 200)],
                            ["Bytes", str(len(data))]])],
               {"remote_path": remote_path, "bytes": len(data)})


def _entry(fn, description, input_schema, scope, timeout):
    return {
        "function": fn,
        "description": description,
        "input_schema": input_schema,
        "scope": scope,
        "destructive": DESTRUCTIVE_CLASSIFICATION[fn.__name__],
        "retryable": False,
        "timeout": timeout,
    }


_MACHINE_PROP = {"machine_id": {"type": "string",
                                "description": "The machine's label, address, or id (e.g. 'dgx')."}}


TOOL_REGISTRY = {
    "run_job": _entry(
        run_job,
        "Run a job on a registered cluster from INLINE script text (e.g. shell commands): "
        "it is written to the cluster and submitted with sbatch, then tracked asynchronously "
        "— you can leave and come back; the result appears on the canvas and (opt-in) you're "
        "notified when it finishes. Creates new work; not destructive.",
        {"type": "object", "properties": {
            **_MACHINE_PROP,
            "script": {"type": "string", "description": "The job's script body (bash). e.g. 'nvidia-smi\\nsleep 60\\nnvidia-smi'."},
            "job_name": {"type": "string"},
            "partition": {"type": "string"},
            "time_limit": {"type": "string", "description": "Slurm time string, e.g. '00:05:00'."},
            "nodes": {"type": "integer", "minimum": 1},
            "gpus": {"type": "integer", "minimum": 0},
            "account": {"type": "string"},
            "notify_on_finish": {"type": "boolean", "description": "Notify your clients when the job finishes (default true).", "default": True},
        }, "required": ["machine_id", "script"]},
        "tools:write", 60.0),
    "submit_job": _entry(
        submit_job,
        "Submit an EXISTING sbatch script to the Slurm scheduler on a registered cluster, "
        "then track it asynchronously — you can leave and come back; the result appears on "
        "the canvas and (opt-in) you're notified when it finishes. Creates new work; not "
        "destructive.",
        {"type": "object", "properties": {
            **_MACHINE_PROP,
            "script_path": {"type": "string", "description": "Absolute path to an existing sbatch script on the machine."},
            "partition": {"type": "string"},
            "time_limit": {"type": "string", "description": "Slurm time string, e.g. '01:30:00'."},
            "nodes": {"type": "integer", "minimum": 1},
            "gpus": {"type": "integer", "minimum": 0},
            "job_name": {"type": "string"},
            "account": {"type": "string"},
            "notify_on_finish": {"type": "boolean", "description": "Notify your clients when the job finishes (default true).", "default": True},
        }, "required": ["machine_id", "script_path"]},
        "tools:write", 30.0),
    "make_directory": _entry(
        make_directory,
        "Create a directory (mkdir -p) on a registered machine. Not destructive.",
        {"type": "object", "properties": {
            **_MACHINE_PROP,
            "path": {"type": "string", "description": "Absolute directory path to create."},
        }, "required": ["machine_id", "path"]},
        "tools:write", 20.0),
    "upload_file": _entry(
        upload_file,
        "Upload one of your AstralDeep attachments to an absolute path on a registered machine. "
        "Destructive only if the destination already has content (you'll be asked to confirm).",
        {"type": "object", "properties": {
            **_MACHINE_PROP,
            "attachment_id": {"type": "string", "description": "Id of an attachment in your files."},
            "remote_path": {"type": "string", "description": "Absolute destination path on the machine."},
        }, "required": ["machine_id", "attachment_id", "remote_path"]},
        "tools:write", 60.0),
    "cancel_job": _entry(
        cancel_job,
        "Cancel one of your Slurm jobs (scancel) on a registered cluster. Destructive; asks to confirm.",
        {"type": "object", "properties": {
            **_MACHINE_PROP,
            "job_id": {"type": "string", "description": "Numeric Slurm job id."},
        }, "required": ["machine_id", "job_id"]},
        "tools:write", 20.0),
    "remove_path": _entry(
        remove_path,
        "Delete a file or directory on a registered machine. Destructive; asks to confirm.",
        {"type": "object", "properties": {
            **_MACHINE_PROP,
            "path": {"type": "string", "description": "Absolute path to delete."},
            "recursive": {"type": "boolean", "description": "Delete directories and their contents.", "default": False},
        }, "required": ["machine_id", "path"]},
        "tools:write", 30.0),
    "control_service": _entry(
        control_service,
        "Control a systemd service (start/stop/restart/enable/disable) on a registered machine. "
        "Destructive for stop/disable/restart; asks to confirm those.",
        {"type": "object", "properties": {
            **_MACHINE_PROP,
            "service_name": {"type": "string"},
            "action": {"type": "string", "enum": list(_SERVICE_ACTIONS)},
        }, "required": ["machine_id", "service_name", "action"]},
        "tools:system", 30.0),
    "manage_package": _entry(
        manage_package,
        "Install or remove a system package on a registered machine. Destructive for remove; "
        "asks to confirm removal.",
        {"type": "object", "properties": {
            **_MACHINE_PROP,
            "package_name": {"type": "string"},
            "action": {"type": "string", "enum": list(_PACKAGE_ACTIONS)},
        }, "required": ["machine_id", "package_name", "action"]},
        "tools:system", 120.0),
    "signal_process": _entry(
        signal_process,
        "Send a termination signal (TERM or KILL) to a process on a registered machine. "
        "Destructive; asks to confirm.",
        {"type": "object", "properties": {
            **_MACHINE_PROP,
            "pid": {"type": "string", "description": "Numeric process id."},
            "signal": {"type": "string", "enum": list(_SIGNALS)},
        }, "required": ["machine_id", "pid", "signal"]},
        "tools:system", 15.0),
}
