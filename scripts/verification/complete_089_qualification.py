#!/usr/bin/env python3
"""Feature 089: finish the qualification that needed an authenticated human.

Section 7c of verification.md originally said no chat turn could run on the
local stack, and that unblocking it needed a realm administrator. Both halves
were wrong. The realm already has a registered local redirect URI -- under
``localhost``, not the ``127.0.0.1`` every run happened to use -- and the
device authorization grant needs no redirect URI at all.

What is genuinely required is a person signing in once. That is the design
working, not a defect, and it is why **the owner runs this, not the
implementer**: the one step nobody else can take is approving the code with
your own realm account.

The token is held in memory in this process only. Nothing here prints it and
nothing writes it down.

Usage (from the repository root, with the candidate stack up)::

    docker cp scripts/verification/complete_089_qualification.py         astraldeep:/tmp/complete_089.py
    docker exec -it astraldeep python /tmp/complete_089.py

It prints a URL and a short code. Open the URL, approve, and the rest runs
unattended: it registers over the WebSocket the way a native client does,
opens the LLM settings surface, and drives one real turn. The JSON report it
writes to ``/tmp/089_report.json`` is what T021, T069, T004 and T032 need.

This is deliberately a first probe rather than the whole walkthrough. It
establishes that the gate opens and captures the exact frame shapes the
remaining assertions have to be written against -- those frame shapes have
never been observed on this stack, and guessing them into a longer script
would only produce confident-looking noise.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

AUTH = (os.getenv("KEYCLOAK_AUTHORITY") or "").rstrip("/")
DEVICE_CLIENT = os.getenv("DEVICE_CLIENT", "astral-watch")
WS_URI = os.getenv("WS_URI", "ws://127.0.0.1:8001/ws")
OUT = os.getenv("REPORT", "/tmp/089_report.json")


def _post(url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"error": "non_json", "raw": raw[:300]}


def say(*parts):
    print(*parts, flush=True)


def device_login():
    """RFC 8628 against the public watch client. Returns an access token."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(40)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    status, d = _post(AUTH + "/protocol/openid-connect/auth/device", {
        "client_id": DEVICE_CLIENT, "scope": "openid profile email",
        "code_challenge": challenge, "code_challenge_method": "S256"})
    if status != 200 or "device_code" not in d:
        raise SystemExit("device authorization refused: " + json.dumps(d)[:300])

    say("")
    say("=" * 72)
    say("  SIGN IN ONCE TO FINISH THE 089 QUALIFICATION")
    say("")
    say("  Open:", d.get("verification_uri_complete") or d.get("verification_uri"))
    say("  Code:", d.get("user_code"))
    say("")
    say("  Valid for " + str(d.get("expires_in")) + "s. The rest runs unattended.")
    say("=" * 72)
    say("")

    interval = float(d.get("interval", 5) or 5)
    deadline = time.time() + float(d.get("expires_in", 600) or 600)
    while time.time() < deadline:
        time.sleep(interval)
        status, t = _post(AUTH + "/protocol/openid-connect/token", {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": d["device_code"], "client_id": DEVICE_CLIENT,
            "code_verifier": verifier})
        if status == 200 and t.get("access_token"):
            say("approved.")
            return t["access_token"]
        err = t.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        raise SystemExit(
            "device login failed: " + str(err) + " "
            + str(t.get("error_description", "")))
    raise SystemExit("device code expired without approval")


def claims_of(token):
    """Non-validating decode, for the report. The stack does the real check."""
    try:
        part = token.split(".")[1]
        part += "=" * ((4 - len(part) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(part).decode())
    except Exception:
        return {}


PROGRESS = {"chat_response", "chat_chunk", "assistant_message", "status",
            "thinking", "tool_call", "render", "ui_update", "component"}

MARKER = 'data-ui-action="'


async def probe(token):
    import websockets

    report = {"steps": []}
    async with websockets.connect(WS_URI, max_size=50 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "type": "register_ui", "token": token,
            "capabilities": ["text", "images"],
            "session_id": "089-complete-" + uuid.uuid4().hex[:8],
        }))

        # --- the gate itself ------------------------------------------------
        registered = False
        frames = []
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                break
            f = json.loads(raw)
            frames.append(f.get("type"))
            if f.get("type") == "system_config":
                registered = True
                break
            if f.get("type") in ("auth_required", "error"):
                report["steps"].append({"step": "register_ui", "ok": False,
                                        "detail": str(f)[:400]})
                report["registered"] = False
                return report
        report["registered"] = registered
        report["steps"].append({"step": "register_ui", "ok": registered,
                                "frames": frames[:20]})
        if not registered:
            return report

        async def collect(seconds):
            got = []
            stop = time.monotonic() + seconds
            while time.monotonic() < stop:
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=max(0.1, stop - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                try:
                    got.append(json.loads(raw))
                except Exception:
                    pass
            return got

        # --- the LLM settings surface (T021 / T069 reconnaissance) ----------
        await ws.send(json.dumps({"type": "ui_event", "action": "chrome_open",
                                  "payload": {"surface": "llm"}}))
        got = await collect(20)
        shapes = [{"type": f.get("type"), "keys": sorted(f.keys())[:12],
                   "len": len(json.dumps(f))} for f in got]
        html = ""
        for f in got:
            for key in ("html", "content", "body", "markup"):
                v = f.get(key)
                if isinstance(v, str) and len(v) > len(html):
                    html = v
        low = html.lower()
        report["steps"].append({
            "step": "chrome_open:llm", "frames": shapes, "html_len": len(html),
            "has_typesafe_section": "typesafe" in low,
            "has_data_sharing": ("data_sharing" in low
                                 or "how your data is shared" in low),
            "actions": sorted(set(
                p.split('"')[0] for p in html.split(MARKER)[1:]))[:20],
        })
        with open("/tmp/089_llm_surface.html", "w", encoding="utf-8") as fh:
            fh.write(html)

        # --- one real turn (T004 end-to-end / T032 SC-002) ------------------
        sent = time.perf_counter()
        await ws.send(json.dumps({
            "type": "ui_event", "action": "chat_message",
            "payload": {"message": "What's the 7-day forecast for Lexington, KY?"}}))
        first = None
        seen = []
        done = False
        stop = time.monotonic() + 120
        while time.monotonic() < stop:
            try:
                raw = await asyncio.wait_for(
                    ws.recv(), timeout=max(0.1, stop - time.monotonic()))
            except asyncio.TimeoutError:
                break
            f = json.loads(raw)
            seen.append(f.get("type"))
            if first is None and f.get("type") in PROGRESS:
                first = (time.perf_counter() - sent) * 1000.0
            if f.get("type") in ("turn_complete", "chat_complete"):
                done = True
                break
            if f.get("type") == "error":
                report["turn_error"] = str(f)[:500]
                break
        report["steps"].append({
            "step": "chat_message", "completed": done,
            "to_first_frame_ms": round(first, 1) if first else None,
            "frames": seen[:40]})
    return report


def main():
    if not AUTH:
        raise SystemExit("KEYCLOAK_AUTHORITY is not set in this container")
    token = device_login()
    c = claims_of(token)
    say("token claims: azp=" + str(c.get("azp"))
        + " sub=" + str(c.get("sub"))[:8] + "..."
        + " roles=" + str(sorted(c.get("realm_access", {}).get("roles", []))[:6]))
    report = asyncio.run(probe(token))
    report["azp"] = c.get("azp")
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    say("")
    say(json.dumps(report, indent=2)[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
