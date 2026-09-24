#!/usr/bin/env python3
"""Owner-run walkthrough that signs in once via device grant, then drives the whole
qualification (first-run dialog, TypeSafe key save/clear, chat turns) unattended over
the orchestrator's websocket.
"""
from __future__ import annotations

import asyncio
import base64
import getpass
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
from datetime import datetime, timezone

AUTH = (os.getenv("KEYCLOAK_AUTHORITY") or "").rstrip("/")
DEVICE_CLIENT = os.getenv("DEVICE_CLIENT", "astral-watch")
WS_URI = os.getenv("WS_URI", "ws://127.0.0.1:8001/ws")
OUT = os.getenv("REPORT", "/tmp/089_report.json")

STATUS_UNSET = "Not set — standard routing"
STATUS_ACTIVE = "Active"
PLACEHOLDER_HIDDEN = "Saved key hidden"
SAVED_OK = "TypeSafe key saved."
REJECTED = "TypeSafe rejected that key."
ACK_FIELD = "data_sharing_acknowledged"
ACK_ERROR = "Check this box to confirm you understand how your data is shared."
KEY_FIELD = "typesafe_api_key"

PROGRESS = {"chat_response", "chat_chunk", "assistant_message", "status",
            "thinking", "tool_call", "render", "ui_render", "ui_update",
            "component", "chrome_render"}
TERMINAL = ("turn_complete", "chat_complete")


def say(*parts):
    print(*parts, flush=True)


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


def device_login():
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
    say("  Valid for " + str(d.get("expires_in")) + "s.")
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


def password_grant(user, password, client):
    form = {"grant_type": "password", "client_id": client,
            "username": user, "password": password,
            "scope": "openid profile email"}
    secret = os.getenv("KC_SECRET", "")
    if secret:
        form["client_secret"] = secret
    status, t = _post(AUTH + "/protocol/openid-connect/token", form)
    if status != 200 or not t.get("access_token"):
        raise SystemExit("password grant refused: " + json.dumps(t)[:300])
    say("signed in as " + user + " against " + AUTH)
    return t["access_token"]


def claims_of(token):
    try:
        part = token.split(".")[1]
        part += "=" * ((4 - len(part) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(part).decode())
    except Exception:
        return {}


# Must be real uuid4s; dispatch silently drops otherwise
def ui_event(action, payload):
    return {"type": "ui_event", "action": action, "payload": payload,
            "submission_id": str(uuid.uuid4()),
            "request_generation": str(uuid.uuid4())}


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


class Walk:
    def __init__(self, ws, key):
        self.ws = ws
        self.key = key
        self.steps = []

    def record(self, task, step, ok, detail, **extra):
        row = {"task": task, "step": step, "ok": bool(ok), "detail": detail}
        row.update(extra)
        self.steps.append(row)
        say(("  PASS  " if ok else "  FAIL  ") + task + " / " + step + " -- " + detail)

    async def collect(self, seconds):
        got = []
        stop = time.monotonic() + seconds
        while time.monotonic() < stop:
            try:
                raw = await asyncio.wait_for(
                    self.ws.recv(), timeout=max(0.1, stop - time.monotonic()))
            except asyncio.TimeoutError:
                break
            try:
                got.append(json.loads(raw))
            except Exception:
                pass
        return got

    async def act(self, action, payload, seconds=20.0):
        await self.ws.send(json.dumps(ui_event(action, payload)))
        frames = await self.collect(seconds)
        html = ""
        for f in frames:
            for key in ("html", "content", "body", "markup"):
                v = f.get(key)
                if isinstance(v, str) and len(v) > len(html):
                    html = v
        return frames, html

    async def first_run(self, creds):
        _, html = await self.act("chrome_open", {"surface": "llm"})
        mandatory = 'data-mandatory="1"' in html
        self.record("T069", "first-run dialog shown", mandatory,
                    "a new account is met by the mandatory dialog" if mandatory
                    else "no mandatory dialog; this account is already set up")
        self.record("T069", "acknowledgment on the first-run dialog",
                    ACK_FIELD in html,
                    "the data-sharing field is on the first-run dialog"
                    if ACK_FIELD in html else "no " + ACK_FIELD + " field")
        self.record("T069", "first-run dialog has a save action",
                    "chrome_llm_save" in html,
                    "the dialog offers a save action"
                    if "chrome_llm_save" in html
                    else "MANDATORY DIALOG WITH NO SAVE ACTION")

        api_key = creds.get("OPENAI_API_KEY", "")
        base = {"provider": creds.get("LLM_PROVIDER", "custom"),
                "api_key": api_key,
                "base_url": creds.get("OPENAI_BASE_URL", ""),
                "model": creds.get("LLM_MODEL", "")}
        if not api_key:
            self.record("T069", "acknowledgment gate", False,
                        "no provider credential supplied; the gate cannot be "
                        "exercised")
            return

        fields = dict(base)
        fields[ACK_FIELD] = False
        _, html = await self.act("chrome_llm_save", {"fields": fields}, 60)
        self.record("T069", "save without acknowledgment", ACK_ERROR in html,
                    "refused with the documented message" if ACK_ERROR in html
                    else "expected the acknowledgment refusal; got: " + html[:200])

        fields = dict(base)
        fields[ACK_FIELD] = True
        _, html = await self.act("chrome_llm_save", {"fields": fields}, 90)
        ok = "saved" in html.lower() and "rejected" not in html.lower()
        self.record("T069", "save with acknowledgment", ok,
                    "the save proceeds once acknowledged" if ok
                    else "save did not report success: " + html[:200])
        self.record("FR-035", "provider key never echoed", api_key not in html,
                    "the provider key does not appear in the surface"
                    if api_key not in html else "KEY MATERIAL IN THE SURFACE")

    async def settings(self):
        _, html = await self.act("chrome_open", {"surface": "llm"})
        self.record("T021", "open settings", "typesafe" in html.lower(),
                    "TypeSafe section present" if "typesafe" in html.lower()
                    else "no TypeSafe section in the rendered surface",
                    html_len=len(html))
        self.record("T021", "initial status", STATUS_UNSET in html,
                    "status reads " + repr(STATUS_UNSET) if STATUS_UNSET in html
                    else "initial status is not the unset line (a key may "
                         "already be saved for this user)")
        self.record("T021", "acknowledgment present", ACK_FIELD in html,
                    "the data-sharing field is on the surface" if ACK_FIELD in html
                    else "no " + ACK_FIELD + " field found")

        _, html = await self.act("chrome_typesafe_save", {
            "fields": {KEY_FIELD: "sk-invalid-089-walkthrough", ACK_FIELD: True}}, 45)
        rejected = REJECTED in html or "reject" in html.lower()
        self.record("T021", "invalid key rejected", rejected,
                    "rejected" if rejected else "expected a rejection; got: "
                    + html[:200])

        _, html = await self.act("chrome_typesafe_save", {
            "fields": {KEY_FIELD: self.key, ACK_FIELD: True}}, 45)
        saved = SAVED_OK in html or "saved" in html.lower()
        self.record("T021", "real key saved", saved,
                    "saved" if saved else "save did not report success: " + html[:200])
        self.record("FR-035", "key never echoed", self.key not in html,
                    "the key does not appear in the rendered surface"
                    if self.key not in html else "KEY MATERIAL IN THE SURFACE")

        _, html = await self.act("chrome_open", {"surface": "llm"})
        active = STATUS_ACTIVE in html
        self.record("T021", "status active", active,
                    "status reads Active" if active else "status is not Active: "
                    + html[:200])
        self.record("T021", "saved key hidden", PLACEHOLDER_HIDDEN in html,
                    "the field shows the hidden placeholder"
                    if PLACEHOLDER_HIDDEN in html else "no hidden placeholder")
        self.record("FR-035", "key never echoed on reopen", self.key not in html,
                    "the key does not appear on reopen"
                    if self.key not in html else "KEY MATERIAL IN THE SURFACE")

        _, html = await self.act("chrome_typesafe_clear", {"fields": {}}, 30)
        _, html2 = await self.act("chrome_open", {"surface": "llm"})
        unset = STATUS_UNSET in html2
        self.record("T021", "key removed", unset,
                    "status is back to the unset line" if unset
                    else "status after removal: " + html2[:200])

        _, html = await self.act("chrome_typesafe_save", {
            "fields": {KEY_FIELD: self.key, ACK_FIELD: True}}, 45)
        self.record("T021", "re-saved for the turn phase",
                    SAVED_OK in html or "saved" in html.lower(),
                    "re-saved")

    async def turns(self, count):
        prompts = [
            "What's the 7-day forecast for Lexington, KY?",
            "Summarize the current conditions in Lexington, KY.",
            "What's the wind speed in Lexington right now?",
        ]
        first_frames = []
        completed = 0
        hung = 0
        started = now_utc()
        for i in range(count):
            sent = time.perf_counter()
            await self.ws.send(json.dumps(ui_event(
                "chat_message", {"message": prompts[i % len(prompts)]})))
            first = None
            done = False
            stop = time.monotonic() + 120
            while time.monotonic() < stop:
                try:
                    raw = await asyncio.wait_for(
                        self.ws.recv(), timeout=max(0.1, stop - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                f = json.loads(raw)
                if first is None and f.get("type") in PROGRESS:
                    first = (time.perf_counter() - sent) * 1000.0
                if f.get("type") in TERMINAL:
                    done = True
                    break
                if f.get("type") == "error":
                    break
            if first is not None:
                first_frames.append(first)
            if done:
                completed += 1
            else:
                hung += 1
            say("  turn " + str(i + 1) + "/" + str(count)
                + (" first frame " + str(round(first, 1)) + " ms"
                   if first else " no progress frame"))
        ended = now_utc()
        first_frames.sort()

        def pct(p):
            if not first_frames:
                return None
            k = min(len(first_frames) - 1, int(round(p * (len(first_frames) - 1))))
            return round(first_frames[k], 1)

        self.record("T032", "turns driven", completed > 0,
                    str(completed) + " completed, " + str(hung) + " without a "
                    "terminal frame", turns=count, completed=completed,
                    hung=hung, p50_ms=pct(0.5), p95_ms=pct(0.95),
                    window_utc=[started, ended])
        return {"started_utc": started, "ended_utc": ended,
                "to_first_frame_ms": [round(x, 1) for x in first_frames],
                "completed": completed, "hung": hung}


async def run(token, creds, turns):
    import websockets

    report = {"steps": [], "turns": None}
    async with websockets.connect(WS_URI, max_size=50 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "type": "register_ui", "token": token,
            "capabilities": ["text", "images"],
            "session_id": "089-complete-" + uuid.uuid4().hex[:8],
        }))
        registered = False
        deadline = time.monotonic() + 45
        refusal = ""
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                break
            f = json.loads(raw)
            if f.get("type") == "system_config":
                registered = True
                break
            if f.get("type") in ("auth_required", "error"):
                refusal = str(f)[:400]
                break
        report["registered"] = registered
        if not registered:
            report["steps"].append({
                "task": "§7c", "step": "register_ui", "ok": False,
                "detail": refusal or "no system_config within 45 s"})
            say("register_ui refused: " + (refusal or "timed out"))
            return report
        say("registered over the socket; the 088 gate accepted the token.")
        say("")

        walk = Walk(ws, creds.get("TYPESAFE_API_KEY", ""))
        await walk.first_run(creds)
        say("")
        await walk.settings()
        say("")
        report["turns"] = await walk.turns(turns)
        report["steps"] = walk.steps
    return report


def main():
    if not AUTH:
        raise SystemExit("KEYCLOAK_AUTHORITY is not set in this container")
    turns = int(os.getenv("TURNS", "30"))
    if "--turns" in sys.argv:
        turns = int(sys.argv[sys.argv.index("--turns") + 1])

    # Read stdin before the device wait, not after
    creds = {}
    if sys.stdin is not None and sys.stdin.isatty():
        say("Paste the TypeSafe API key.")
        creds["TYPESAFE_API_KEY"] = getpass.getpass("TypeSafe key (hidden): ").strip()
        say("No provider credential on a terminal run: the signed-in account "
            "must already have one configured, or the turns will not run.")
    else:
        wanted = ("TYPESAFE_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
                  "LLM_MODEL", "LLM_PROVIDER")
        for line in sys.stdin:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            name = name.strip()
            if name in wanted:
                creds[name] = value.strip().strip('"').strip("'")
        say("read " + str(len(creds)) + " of " + str(len(wanted))
            + " known settings from stdin: " + ", ".join(sorted(creds)))
    if not creds.get("TYPESAFE_API_KEY"):
        raise SystemExit(
            "no TYPESAFE_API_KEY on the terminal or stdin. Pipe the owner env "
            "file in, or run with -it to be prompted.")

    kc_user = os.getenv("KC_USER", "")
    if kc_user:
        token = password_grant(kc_user, os.getenv("KC_PASS", ""),
                               os.getenv("KC_CLIENT", "astral-frontend"))
    else:
        token = device_login()
    c = claims_of(token)
    say("token claims: azp=" + str(c.get("azp"))
        + " sub=" + str(c.get("sub"))[:8] + "...")
    say("")
    report = asyncio.run(run(token, creds, turns))
    report["azp"] = c.get("azp")
    ok = [s for s in report["steps"] if s.get("ok")]
    report["summary"] = {"passed": len(ok), "of": len(report["steps"])}
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    say("")
    say("wrote " + OUT + " -- " + str(len(ok)) + "/" + str(len(report["steps"]))
        + " checks passed")
    if report.get("turns"):
        say("")
        say("Now read the server-side overlap window on the HOST, for SC-002:")
        say("  python scripts/verification/typesafe_turn_timeline.py "
            "--perf-only --since " + report["turns"]["started_utc"])
    return 0 if all(s.get("ok") for s in report["steps"]) else 1


if __name__ == "__main__":
    sys.exit(main())
