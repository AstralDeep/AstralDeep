"""Live REAL-AUTH end-to-end verification of the native Windows client.

Proves the production chain against a running orchestrator with
USE_MOCK_AUTH=false:

  real OIDC desktop login (dedicated public client, azp=astral-desktop)
    -> register_ui (device_type=windows, native supported_types)
    -> chat_message -> ReAct loop -> astralprims -> ROTE(windows)
    -> ui_render/ui_upsert(components) -> native PySide6 widgets
    -> client-hosted Windows tools agent (register + tool call)

It connects with the EXACT device caps + native render vocabulary the GUI sends,
so it is a faithful headless stand-in for AstralDeep.exe at the wire level.

Not a pytest test (needs a live server + an interactive browser login the first
time). Usage:

    python tests/verify_live.py --authority https://iam.ai.uky.edu/realms/Astral \
        --prompt "roll 3 dice and show the results"

The obtained token is cached to --token-file (default .astral_token.json) so
re-runs and the GUI (`AstralDeep.exe --token ...`) can reuse it within its TTL.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import websockets  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from astral_client.protocol import device_caps  # noqa: E402
from astral_client.renderer import RenderContext, render, supported_types  # noqa: E402


def _http_base(ws_url: str) -> str:
    from urllib.parse import urlparse
    u = urlparse(ws_url)
    return f"{'https' if u.scheme == 'wss' else 'http'}://{u.netloc}"


def resolve_token(args) -> str:
    if args.token:
        return args.token
    if args.token_file and os.path.exists(args.token_file) and not args.authority:
        with open(args.token_file) as f:
            return json.load(f)["access_token"]
    if args.authority:
        from astral_client.auth import oidc_login
        print(f"[auth] opening browser for OIDC login (client={args.client_id})…")
        sess = oidc_login(args.authority, client_id=args.client_id)
        if args.token_file:
            with open(args.token_file, "w") as f:
                json.dump({"access_token": sess.access_token,
                           "refresh_token": sess.refresh_token}, f)
            print(f"[auth] token cached to {args.token_file}")
        return sess.access_token
    return "dev-token"


def _decode_claims(tok: str) -> dict:
    import base64
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        return {}


async def run(args) -> int:
    token = resolve_token(args)
    claims = _decode_claims(token)
    print(f"[auth] user={claims.get('preferred_username', claims.get('sub', '?'))} "
          f"azp={claims.get('azp', '(none)')} roles={claims.get('realm_access', {}).get('roles', [])}")

    # Host the Windows tools agent exactly like the GUI does, so we can prove the
    # client-hosted A2A path too.
    agent_host = os.getenv("ASTRAL_AGENT_HOST", "host.docker.internal")
    agent_port = int(os.getenv("WIN_AGENT_PORT", "8771"))
    if not args.no_agent:
        try:
            import win_agent.agent as wa
            # The listener now requires a usable AGENT_API_KEY inbound and
            # refuses to start without one. Say so explicitly rather than
            # letting the run appear to prove a path it never exercised.
            thread = wa.start_agent_thread(port=agent_port)
            if thread is None:
                print("[win-agent] listener REFUSED: no usable AGENT_API_KEY "
                      "(16+ ASCII chars) — the client-hosted A2A path is NOT "
                      "being verified by this run")
            else:
                time.sleep(0.6)
                print(f"[win-agent] hosting Windows tools on :{agent_port} "
                      f"(orchestrator reaches it at {agent_host}:{agent_port})")
        except Exception as e:
            print(f"[win-agent] could not start ({e})")

    caps = device_caps(supported_types=supported_types())
    captured, events, chat_id = [], [], None
    agent_registered = False

    async with websockets.connect(args.url, max_size=16 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "type": "register_ui", "token": token, "capabilities": ["render", "stream"],
            "session_id": "win-verify", "device": caps, "resumed": False}))

        # register the client-hosted Windows tools agent (as the GUI does on connect)
        if not args.no_agent:
            await ws.send(json.dumps({"type": "ui_event", "action": "register_external_agent",
                                      "payload": {"url": f"http://{agent_host}:{agent_port}"}}))
        if args.enable_agents:
            await ws.send(json.dumps({"type": "ui_event", "action": "enable_recommended_agents",
                                      "payload": {"source": "verify"}}))
            print("[agents] sent enable_recommended_agents (read-only grant for public agents)")
            await asyncio.sleep(2.0)
        await ws.send(json.dumps({"type": "ui_event", "action": "new_chat", "payload": {}}))

        loop = asyncio.get_event_loop()
        deadline = loop.time() + args.window
        sent = False
        while loop.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - loop.time()))
            except asyncio.TimeoutError:
                break
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            t = msg.get("type")
            events.append(t)
            if t == "auth_required":
                print(f"\nAUTH FAILED: reason={msg.get('reason')!r} — token rejected by the "
                      f"orchestrator (check USE_MOCK_AUTH=false + KEYCLOAK_ALLOWED_AZP=astral-desktop).")
                return 2
            if t == "rote_config":
                prof = msg.get("device_profile", {})
                print(f"[rote] device_profile: type={prof.get('device_type')} "
                      f"surface={prof.get('surface_width')}x{prof.get('surface_height')} "
                      f"voice={prof.get('voice')!r}")
            if t == "chat_status" and "agent" in (msg.get("message") or "").lower():
                if not agent_registered:
                    print(f"[win-agent] {msg.get('message')}")
                    agent_registered = True
            if t == "chat_created" and not sent:
                chat_id = (msg.get("payload") or {}).get("chat_id")
                await ws.send(json.dumps({"type": "ui_event", "action": "chat_message",
                                          "session_id": chat_id,
                                          "payload": {"message": args.prompt, "chat_id": chat_id}}))
                sent = True
                print(f"[chat] asked: {args.prompt!r} (chat={chat_id})")
                deadline = loop.time() + args.window
            if t in ("ui_render", "ui_upsert"):
                captured.append(msg)

    # Flatten every structured component from the real payloads and render natively.
    components = []
    for m in captured:
        if m.get("type") == "ui_render":
            components += [c for c in (m.get("components") or []) if isinstance(c, dict)]
        else:
            for op in m.get("ops") or []:
                if isinstance(op.get("component"), dict):
                    components.append(op["component"])

    app = QApplication.instance() or QApplication([])  # noqa: F841
    ctx = RenderContext(emit=lambda a, p: None)
    rendered = errors = 0
    types: dict[str, int] = {}
    for c in components:
        types[c.get("type", "?")] = types.get(c.get("type", "?"), 0) + 1
        w = render(c, ctx)
        if isinstance(w, QWidget):
            rendered += 1
        if "render error" in (w.text() if hasattr(w, "text") else ""):
            errors += 1

    print("\n================ VERIFY REPORT ================")
    print(f"server events     : {len(events)}  ({', '.join(sorted(set(events)))})")
    print(f"ui payloads       : {len(captured)}  ui_render+ui_upsert")
    print(f"components         : {len(components)}  rendered={rendered} errors={errors}")
    print(f"component types    : {dict(sorted(types.items()))}")
    print(f"windows tools agent: {'registered' if agent_registered else 'not seen'}")
    ok = rendered == len(components) and len(components) > 0 and errors == 0
    print("RESULT:", "PASS — real orchestrator SDUI renders natively over real auth"
          if ok else "INCOMPLETE — see above")
    print("==============================================")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Live real-auth verification of the Windows client")
    ap.add_argument("--url", default=os.getenv("ASTRAL_WS_URL", "ws://127.0.0.1:8001/ws"))
    ap.add_argument("--authority", default=os.getenv("KEYCLOAK_AUTHORITY", ""))
    ap.add_argument("--client-id", default=os.getenv("ASTRAL_CLIENT_ID", "astral-desktop"))
    ap.add_argument("--token", default=os.getenv("ASTRAL_TOKEN", ""))
    ap.add_argument("--token-file", default=".astral_token.json")
    ap.add_argument("--prompt", default="roll 3 dice and show the results")
    ap.add_argument("--window", type=float, default=45.0, help="seconds to wait for each phase")
    ap.add_argument("--no-agent", action="store_true")
    ap.add_argument("--enable-agents", action="store_true",
                    help="send enable_recommended_agents before the chat (read-only grant)")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
