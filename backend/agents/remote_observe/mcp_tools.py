#!/usr/bin/env python3
"""Read-only verbs for remote-observe-1 (feature 063).

Structured, typed fields only (FR-038); every remote string bounded + control/ANSI
sanitised (FR-040/FR-041); no shell strings (FR-022 — the transport uses the argv
login-shell wrapper); every outcome mapped to the result vocabulary (FR-034). These
verbs cannot change remote state (US2 acceptance 4).
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from astralprims import Alert, Card, Table, Text

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


TOOL_REGISTRY = {
    "list_machines": {
        "function": list_machines,
        "description": "List your own registered remote machines and their last-known reachability.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
        "scope": "tools:read",
    },
    "probe_machine": {
        "function": probe_machine,
        "description": "Test whether one of your registered machines is reachable and your credential authenticates.",
        "input_schema": {
            "type": "object",
            "properties": {"machine_id": {"type": "string", "description": "The machine's label, address, or id (e.g. 'dgx')."}},
            "required": ["machine_id"],
        },
        "scope": "tools:read",
    },
    "list_queue": {
        "function": list_queue,
        "description": "List your own jobs in the Slurm queue on a registered cluster machine.",
        "input_schema": {
            "type": "object",
            "properties": {"machine_id": {"type": "string", "description": "The cluster machine's label, address, or id (e.g. 'dgx')."}},
            "required": ["machine_id"],
        },
        "scope": "tools:read",
    },
    "host_facts": {
        "function": host_facts,
        "description": "Report typed host facts (CPUs, load, uptime, memory) for a registered machine.",
        "input_schema": {
            "type": "object",
            "properties": {"machine_id": {"type": "string", "description": "The machine's label, address, or id (e.g. 'dgx')."}},
            "required": ["machine_id"],
        },
        "scope": "tools:read",
    },
}
