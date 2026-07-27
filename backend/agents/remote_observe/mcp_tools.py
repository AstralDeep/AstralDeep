#!/usr/bin/env python3
"""Read-only remote-compute verb library (feature 063) — unioned into remote-compute-1.

Structured, typed fields only (FR-038); every remote string bounded + control/ANSI
sanitised (FR-040/FR-041); no shell strings (FR-022 — the transport uses the argv
login-shell wrapper); every outcome mapped to the result vocabulary (FR-034). These
verbs cannot change remote state (US2 acceptance 4).
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from astralprims import Alert, Card, CodeBlock, Table, Text

from orchestrator import remote_machines
from orchestrator.credential_manager import CredentialNotConfigured, CredentialUndecryptable
from orchestrator.remote_machines import MachineNotFound
from orchestrator.remote_transport import RemoteResult, Verdict, get_transport

# Dependencies wired by RemoteObserveAgent.__init__ (in-process pattern).
_DB = None
_CREDMGR = None

_MAX_FIELD = 256   # per remote string field bound (FR-040)
_MAX_ROWS = 200    # listing bound
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\x1b]")


def register_deps(db, credmgr) -> None:
    """Wire the shared Database + CredentialManager used by the verbs."""
    global _DB, _CREDMGR
    _DB, _CREDMGR = db, credmgr


def _sanitize(value: Any, limit: int = _MAX_FIELD) -> str:
    """Strip control/ANSI chars and bound length — remote strings are DATA (FR-041)."""
    if value is None:
        return ""
    text = _CTRL.sub("", str(value))
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


def _ui(components: List[Any], data: Optional[Dict] = None) -> Dict[str, Any]:
    return {"_ui_components": [c.to_dict() for c in components], "_data": data}


def _ok(title: str, components: List[Any], data: Optional[Dict] = None) -> Dict[str, Any]:
    """Wrap success output in a titled Card so the canvas renders it as a proper
    workspace component (matches the working bundled-agent pattern, e.g. dice_roller —
    a bare Text/Table with no container leaks as stray canvas text)."""
    return {"_ui_components": [Card(title=title, content=components).to_dict()], "_data": data}


def _fail(verdict: Any, machine: str, next_action: str = "") -> Dict[str, Any]:
    v = verdict.value if isinstance(verdict, Verdict) else str(verdict)
    text = f"{_sanitize(machine, 80)}: {v}" + (f" — {next_action}" if next_action else "")
    return _ui([Alert(message=text, variant="error")],
               data={"verdict": v, "machine": machine, "next_action": next_action})


def _resolve(user_id: Optional[str], ref: Optional[str]):
    if not user_id:
        return None, _fail("permission_denied", ref or "?", "sign in to use remote machines")
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


def _result_fail(res: RemoteResult) -> Dict[str, Any]:
    return _fail(res.verdict, res.machine, res.next_action)


# ── list_machines ────────────────────────────────────────────────────────────

def list_machines(**kwargs) -> Dict[str, Any]:
    user_id = kwargs.get("user_id")
    if not user_id:
        return _fail("permission_denied", "-", "sign in to use remote machines")
    rows = remote_machines.list_machines(_DB, user_id)
    if not rows:
        return _ok("Remote machines",
                   [Text(content="You have no registered machines yet — add one under "
                                 "Settings → Remote machines.", variant="body")],
                   {"machines": []})
    table = Table(
        headers=["Label", "Address", "OS", "Role", "Last check"],
        rows=[[_sanitize(r["label"], 60), f'{_sanitize(r["address"], 120)}:{r["port"]}',
               r["os_family"], r["role"], (r.get("last_verdict") or "—")] for r in rows],
    )
    return _ok(f"Remote machines ({len(rows)})", [table],
               {"count": len(rows), "machines": [
                   {"machine_id": r["machine_id"], "label": r["label"],
                    "address": r["address"], "role": r["role"],
                    "last_verdict": r.get("last_verdict")} for r in rows]})


# ── probe_machine ────────────────────────────────────────────────────────────

def probe_machine(**kwargs) -> Dict[str, Any]:
    user_id = kwargs.get("user_id")
    ref = kwargs.get("machine_id") or kwargs.get("machine") or kwargs.get("label")
    target, err = _resolve(user_id, ref)
    if err:
        return err
    res = get_transport().probe(target, timeout=20.0)
    try:
        remote_machines.record_probe(_DB, user_id, target.machine_id, res.verdict.value, host_key=res.host_key)
    except Exception:
        pass
    if not res.ok:
        return _result_fail(res)
    rows = [["Reachable", "yes"], ["Authenticated", "yes"]]
    if res.host_key and res.host_key.get("fingerprint"):
        rows.append(["Host key", _sanitize(res.host_key["fingerprint"], 80)])
    return _ok(f"{_sanitize(target.label, 60)} — reachable & authenticated",
               [Table(headers=["Fact", "Value"], rows=rows)],
               {"verdict": "ok", "authenticated": True})


# ── list_queue (Slurm) ───────────────────────────────────────────────────────

_SQUEUE_ARGV = ["squeue", "--me", "--json"]


def _num(v, default=None):
    if isinstance(v, dict):
        return None if v.get("infinite") else v.get("number", default)
    return v


def _parse_squeue_json(text: str) -> Optional[List[Dict]]:
    try:
        doc = json.loads(text or "")
    except Exception:
        return None
    jobs = []
    for j in (doc.get("jobs") or []):
        state = j.get("job_state")
        if isinstance(state, list):
            state = ",".join(str(s) for s in state) if state else "?"
        jobs.append({
            "id": str(_num(j.get("job_id")) or j.get("job_id") or "?"),
            "name": j.get("name") or "",
            "state": str(state or "?"),
            "partition": j.get("partition") or "",
            "nodes": str(_num(j.get("node_count")) or j.get("node_count") or ""),
            "reason": j.get("state_reason") or "",
        })
    return jobs


def list_queue(**kwargs) -> Dict[str, Any]:
    user_id = kwargs.get("user_id")
    ref = kwargs.get("machine_id") or kwargs.get("machine") or kwargs.get("label")
    target, err = _resolve(user_id, ref)
    if err:
        return err
    res = get_transport().run(target, _SQUEUE_ARGV, timeout=30.0, retryable=True)
    if not res.ok:
        return _result_fail(res)
    jobs = _parse_squeue_json(res.stdout)
    if jobs is None:
        return _fail(Verdict.PARTIAL, target.label, "could not parse the scheduler response")
    if not jobs:
        return _ok(f"Queue — {_sanitize(target.label, 60)}",
                   [Text(content="No jobs in your queue.", variant="body")], {"jobs": 0})
    table = Table(
        headers=["Job", "Name", "State", "Partition", "Nodes", "Reason"],
        rows=[[j["id"], _sanitize(j["name"], 40), j["state"], _sanitize(j["partition"], 24),
               j["nodes"], _sanitize(j["reason"], 40)] for j in jobs[:_MAX_ROWS]],
    )
    comps = [table]
    if len(jobs) > _MAX_ROWS:
        comps.append(Text(content=f"Showing {_MAX_ROWS} of {len(jobs)} jobs.", variant="caption"))
    return _ok(f"Queue — {_sanitize(target.label, 60)} ({len(jobs)} job(s))", comps, {"jobs": len(jobs)})


# ── host_facts ───────────────────────────────────────────────────────────────

def _fmt_uptime(seconds: float) -> str:
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}d {hours}h {mins}m"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def _fmt_bytes(n: int) -> str:
    step = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if step < 1024 or unit == "TiB":
            return f"{step:.1f} {unit}"
        step /= 1024
    return f"{n} B"


def _parse_proc(text: str) -> Dict[str, str]:
    lines = (text or "").splitlines()
    facts: Dict[str, str] = {}
    if lines:
        la = lines[0].split()
        if len(la) >= 3:
            facts["load"] = f"{la[0]} {la[1]} {la[2]}"
    if len(lines) >= 2:
        up = lines[1].split()
        if up:
            try:
                facts["uptime"] = _fmt_uptime(float(up[0]))
            except ValueError:
                pass
    mem_total = mem_avail = None
    for ln in lines[2:]:
        if ln.startswith("MemTotal:"):
            mem_total = _mem_kb(ln)
        elif ln.startswith("MemAvailable:"):
            mem_avail = _mem_kb(ln)
    if mem_total is not None:
        facts["mem_total"] = _fmt_bytes(mem_total * 1024)
    if mem_avail is not None:
        facts["mem_avail"] = _fmt_bytes(mem_avail * 1024)
    return facts


def _mem_kb(line: str) -> Optional[int]:
    parts = line.split()
    if len(parts) >= 2 and parts[1].isdigit():
        return int(parts[1])
    return None


def host_facts(**kwargs) -> Dict[str, Any]:
    user_id = kwargs.get("user_id")
    ref = kwargs.get("machine_id") or kwargs.get("machine") or kwargs.get("label")
    target, err = _resolve(user_id, ref)
    if err:
        return err
    transport = get_transport()
    res = transport.run(target, ["cat", "/proc/loadavg", "/proc/uptime", "/proc/meminfo"],
                        timeout=20.0, retryable=True)
    if not res.ok:
        return _result_fail(res)
    facts = _parse_proc(res.stdout)
    ncpu = transport.run(target, ["nproc"], timeout=15.0, retryable=True)
    if ncpu.ok:
        facts["cpus"] = _sanitize((ncpu.stdout or "").strip(), 16)
    labels = [("cpus", "CPUs"), ("load", "Load (1/5/15)"), ("uptime", "Uptime"),
              ("mem_total", "Memory total"), ("mem_avail", "Memory available")]
    rows = [[label, _sanitize(facts[key], 48)] for key, label in labels if facts.get(key)]
    return _ok(f"Host facts — {_sanitize(target.label, 60)}",
               [Table(headers=["Fact", "Value"], rows=rows)], facts)


# ── job_status / job_history (Slurm squeue/sacct --json) ──────────────────────

def _num_field(container: Dict, *keys) -> str:
    for k in keys:
        if k in container:
            return str(_num(container[k]) if isinstance(container[k], dict) else container[k] or "")
    return ""


def _parse_sacct_json(text: str) -> Optional[List[Dict]]:
    try:
        doc = json.loads(text or "")
    except Exception:
        return None
    jobs = []
    for j in (doc.get("jobs") or []):
        state = (j.get("state") or {})
        cur = state.get("current") if isinstance(state, dict) else state
        if isinstance(cur, list):
            cur = ",".join(str(s) for s in cur) if cur else "?"
        tinfo = j.get("time") or {}
        exit_code = j.get("exit_code") or {}
        rc = exit_code.get("return_code")
        if isinstance(rc, dict):
            rc = rc.get("number")
        jobs.append({
            "id": str(_num(j.get("job_id")) or j.get("job_id") or "?"),
            "name": j.get("name") or "",
            "state": str(cur or "?"),
            "elapsed": _num_field(tinfo, "elapsed"),
            "exit_code": "" if rc is None else str(rc),
            "partition": j.get("partition") or "",
            "end": _num_field(tinfo, "end"),
        })
    return jobs


def job_status(**kwargs) -> Dict[str, Any]:
    user_id = kwargs.get("user_id")
    ref = kwargs.get("machine_id") or kwargs.get("machine") or kwargs.get("label")
    job_id = str(kwargs.get("job_id") or "").strip()
    target, err = _resolve(user_id, ref)
    if err:
        return err
    if not re.match(r"^\d+$", job_id):
        return _fail(Verdict.INVALID_ARGUMENT, target.label, "job_id must be a numeric Slurm job id")
    transport = get_transport()
    live = transport.run(target, ["squeue", "--job", job_id, "--json"], timeout=30.0, retryable=True)
    if not live.ok:
        return _result_fail(live)
    jobs = _parse_squeue_json(live.stdout)
    if jobs:
        j = jobs[0]
        rows = [["Job", j["id"]], ["Name", _sanitize(j["name"], 60)], ["State", j["state"]],
                ["Partition", _sanitize(j["partition"], 40)], ["Nodes", j["nodes"]],
                ["Reason", _sanitize(j["reason"], 60)]]
        return _ok(f"Job {job_id} — {_sanitize(target.label, 60)}",
                   [Table(headers=["Field", "Value"], rows=rows)], {"job_id": job_id, "state": j["state"]})
    # Not in the live queue → it may have finished; ask the accounting DB.
    hist = transport.run(target, ["sacct", "-j", job_id, "--json", "-X"], timeout=30.0, retryable=True)
    if not hist.ok:
        return _result_fail(hist)
    acct = _parse_sacct_json(hist.stdout)
    if not acct:
        return _fail(Verdict.NOT_FOUND, target.label, "no such job in the queue or recent accounting records")
    j = acct[0]
    rows = [["Job", j["id"]], ["Name", _sanitize(j["name"], 60)], ["State", j["state"]],
            ["Elapsed", _sanitize(j["elapsed"], 24)], ["Exit code", _sanitize(j["exit_code"], 16)],
            ["Partition", _sanitize(j["partition"], 40)]]
    return _ok(f"Job {job_id} — {_sanitize(target.label, 60)}",
               [Table(headers=["Field", "Value"], rows=rows)], {"job_id": job_id, "state": j["state"]})


def job_history(**kwargs) -> Dict[str, Any]:
    user_id = kwargs.get("user_id")
    ref = kwargs.get("machine_id") or kwargs.get("machine") or kwargs.get("label")
    try:
        days = int(kwargs.get("days") or 7)
    except (TypeError, ValueError):
        days = 7
    days = max(1, min(days, 30))
    target, err = _resolve(user_id, ref)
    if err:
        return err
    res = get_transport().run(
        target, ["sacct", "--json", "-X", "-S", f"now-{days}days", "-E", "now"],
        timeout=45.0, retryable=True)
    if not res.ok:
        return _result_fail(res)
    jobs = _parse_sacct_json(res.stdout)
    if jobs is None:
        return _fail(Verdict.PARTIAL, target.label, "could not parse the accounting response")
    if not jobs:
        return _ok(f"Job history — {_sanitize(target.label, 60)}",
                   [Text(content=f"No jobs in the last {days} day(s).", variant="body")], {"jobs": 0})
    table = Table(
        headers=["Job", "Name", "State", "Elapsed", "Exit", "Partition"],
        rows=[[j["id"], _sanitize(j["name"], 36), j["state"], _sanitize(j["elapsed"], 20),
               _sanitize(j["exit_code"], 12), _sanitize(j["partition"], 24)] for j in jobs[:_MAX_ROWS]],
    )
    comps = [table]
    if len(jobs) > _MAX_ROWS:
        comps.append(Text(content=f"Showing {_MAX_ROWS} of {len(jobs)} jobs.", variant="caption"))
    return _ok(f"Job history — {_sanitize(target.label, 60)} ({len(jobs)} in {days}d)", comps,
               {"jobs": len(jobs), "days": days})


# ── list_directory / list_processes ───────────────────────────────────────────

def _parse_find(text: str) -> List[Dict]:
    entries = []
    for ln in (text or "").splitlines():
        parts = ln.split("\t")
        if len(parts) < 4:
            continue
        ftype, size, mtime, name = parts[0], parts[1], parts[2], "\t".join(parts[3:])
        kind = {"f": "file", "d": "dir", "l": "link"}.get(ftype, ftype)
        try:
            size_bytes = int(size)
        except ValueError:
            size_bytes = None
        entries.append({"type": kind, "size_bytes": size_bytes, "mtime": mtime.split(".")[0], "name": name})
    return entries


def list_directory(**kwargs) -> Dict[str, Any]:
    user_id = kwargs.get("user_id")
    ref = kwargs.get("machine_id") or kwargs.get("machine") or kwargs.get("label")
    path = kwargs.get("path")
    target, err = _resolve(user_id, ref)
    if err:
        return err
    if not (isinstance(path, str) and path.startswith("/") and "\x00" not in path and len(path) <= 4096):
        return _fail(Verdict.INVALID_ARGUMENT, target.label, "path must be an absolute path")
    res = get_transport().run(
        target, ["find", path, "-maxdepth", "1", "-mindepth", "1", "-printf", "%y\\t%s\\t%T@\\t%f\\n"],
        timeout=30.0, retryable=True)
    if not res.ok:
        return _result_fail(res)
    entries = _parse_find(res.stdout)
    if not entries:
        return _ok(f"{_sanitize(path, 80)} — {_sanitize(target.label, 60)}",
                   [Text(content="Empty (or not a directory).", variant="body")], {"entries": 0})
    shown = entries[:_MAX_ROWS]
    table = Table(
        headers=["Type", "Name", "Size"],
        rows=[[e["type"], _sanitize(e["name"], 80),
               (_fmt_bytes(e["size_bytes"]) if e["size_bytes"] is not None else "")] for e in shown],
    )
    comps = [table]
    if len(entries) > _MAX_ROWS:
        comps.append(Text(content=f"Showing {_MAX_ROWS} of {len(entries)} entries.", variant="caption"))
    return _ok(f"{_sanitize(path, 80)} — {_sanitize(target.label, 60)} ({len(entries)})", comps,
               {"path": path, "shown": len(shown), "total": len(entries),
                "truncated": len(entries) > _MAX_ROWS})


def _parse_ps(text: str) -> List[Dict]:
    procs = []
    for ln in (text or "").splitlines():
        parts = ln.split(None, 5)
        if len(parts) < 6:
            continue
        pid, user, cpu, mem, rss, comm = parts
        try:
            rss_bytes = int(rss) * 1024
        except ValueError:
            rss_bytes = None
        procs.append({"pid": pid, "user": user, "cpu_pct": cpu, "mem_pct": mem,
                      "rss_bytes": rss_bytes, "comm": comm})
    return procs


def list_processes(**kwargs) -> Dict[str, Any]:
    user_id = kwargs.get("user_id")
    ref = kwargs.get("machine_id") or kwargs.get("machine") or kwargs.get("label")
    own_only = kwargs.get("own_only")
    own_only = True if own_only is None else bool(own_only)
    target, err = _resolve(user_id, ref)
    if err:
        return err
    fmt = "pid,user:20,pcpu,pmem,rss,comm"
    if own_only:
        argv = ["ps", "-u", target.username, "-o", fmt, "--no-headers"]
    else:
        argv = ["ps", "-eo", fmt, "--no-headers"]
    res = get_transport().run(target, argv, timeout=30.0, retryable=True)
    if not res.ok:
        return _result_fail(res)
    procs = _parse_ps(res.stdout)
    if not procs:
        return _ok(f"Processes — {_sanitize(target.label, 60)}",
                   [Text(content="No matching processes.", variant="body")], {"processes": 0})
    table = Table(
        headers=["PID", "User", "CPU%", "MEM%", "RSS", "Command"],
        rows=[[p["pid"], _sanitize(p["user"], 24), p["cpu_pct"], p["mem_pct"],
               (_fmt_bytes(p["rss_bytes"]) if p["rss_bytes"] is not None else ""),
               _sanitize(p["comm"], 60)] for p in procs[:_MAX_ROWS]],
    )
    comps = [table]
    if len(procs) > _MAX_ROWS:
        comps.append(Text(content=f"Showing {_MAX_ROWS} of {len(procs)} processes.", variant="caption"))
    return _ok(f"Processes — {_sanitize(target.label, 60)} ({len(procs)})", comps,
               {"processes": len(procs), "own_only": own_only})


# ── read_job_output (US4 — bounded, sanitized tail of a tracked job's output) ──

def read_job_output(**kwargs) -> Dict[str, Any]:
    """Read the recent output (tail) of one of the user's own tracked jobs. Surfaces
    the job's OWN stdout — bounded (tail -n) + control-char-sanitized + length-clipped.
    Reads only; resolves the output file from the tracked_job row (or an explicit
    absolute output_path)."""
    user_id = kwargs.get("user_id")
    ref = kwargs.get("machine_id") or kwargs.get("machine") or kwargs.get("label")
    job_id = str(kwargs.get("job_id") or "").strip()
    target, err = _resolve(user_id, ref)
    if err:
        return err
    path = None
    if job_id:
        try:
            from orchestrator import remote_jobs
            row = remote_jobs.get_by_job(_DB, user_id, job_id)
            if row:
                path = row.get("output_path")
        except Exception:
            path = None
    if not path:
        path = kwargs.get("output_path")
    if not (isinstance(path, str) and path.startswith("/") and "\x00" not in path and len(path) <= 4096):
        return _fail(Verdict.NOT_FOUND, target.label,
                     "no tracked output for that job — pass an absolute output_path")
    try:
        lines = max(1, min(int(kwargs.get("lines") or 200), 1000))
    except (TypeError, ValueError):
        lines = 200
    res = get_transport().run(target, ["tail", "-n", str(lines), path], timeout=20.0, retryable=True)
    if not res.ok:
        return _result_fail(res)
    title = (f"Job {job_id} output" if job_id else "Job output") + f" — {_sanitize(target.label, 60)}"
    text = _sanitize(res.stdout or "", 16000)
    if not text.strip():
        return _ok(title, [Text(content="(no output yet)", variant="body")],
                   {"job_id": job_id, "bytes": 0})
    # CodeBlock (<pre>) preserves newlines + monospace so terminal output stays
    # readable; a plain Text would collapse the whole thing onto one line.
    return _ok(title, [CodeBlock(code=text, language="")],
               {"job_id": job_id, "bytes": len(res.stdout or "")})


_M = {"machine_id": {"type": "string", "description": "The machine's label, address, or id (e.g. 'dgx')."}}


def _read_entry(fn, description, properties, required, timeout):
    """A read verb entry. All read verbs are idempotent → ``retryable=True``; each
    declares its timeout so the FR-051 contract test can assert it uniformly."""
    return {
        "function": fn,
        "description": description,
        "input_schema": {"type": "object", "properties": properties, "required": required},
        "scope": "tools:read",
        "retryable": True,
        "timeout": timeout,
    }


TOOL_REGISTRY = {
    "list_machines": _read_entry(
        list_machines,
        "List your own registered remote machines and their last-known reachability.",
        {}, [], 5.0),
    "probe_machine": _read_entry(
        probe_machine,
        "Test whether one of your registered machines is reachable and your credential authenticates.",
        dict(_M), ["machine_id"], 20.0),
    "list_queue": _read_entry(
        list_queue,
        "List your own jobs in the Slurm queue on a registered cluster machine.",
        dict(_M), ["machine_id"], 30.0),
    "host_facts": _read_entry(
        host_facts,
        "Report typed host facts (CPUs, load, uptime, memory) for a registered machine.",
        dict(_M), ["machine_id"], 30.0),
    "job_status": _read_entry(
        job_status,
        "Report the current status of one of your Slurm jobs (live queue, then accounting if finished).",
        {**_M, "job_id": {"type": "string", "description": "Numeric Slurm job id."}},
        ["machine_id", "job_id"], 30.0),
    "job_history": _read_entry(
        job_history,
        "List your recent Slurm jobs from accounting (default last 7 days, max 30).",
        {**_M, "days": {"type": "integer", "minimum": 1, "maximum": 30, "default": 7}},
        ["machine_id"], 45.0),
    "list_directory": _read_entry(
        list_directory,
        "List the entries of a directory (one level) on a registered machine.",
        {**_M, "path": {"type": "string", "description": "Absolute directory path to list."}},
        ["machine_id", "path"], 30.0),
    "list_processes": _read_entry(
        list_processes,
        "List processes on a registered machine (defaults to your own).",
        {**_M, "own_only": {"type": "boolean", "description": "Only your own processes (default true).", "default": True}},
        ["machine_id"], 30.0),
    "read_job_output": _read_entry(
        read_job_output,
        "Read the recent output (tail) of one of your tracked cluster jobs.",
        {**_M,
         "job_id": {"type": "string", "description": "Numeric Slurm job id you submitted."},
         "output_path": {"type": "string", "description": "Absolute output path (if not a tracked job)."},
         "lines": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200}},
        ["machine_id"], 25.0),
}
