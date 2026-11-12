"""Drives a booted personal node as its owner: auth refusals, chat creation, provider save, one tool turn and history readback."""
import asyncio, json, uuid, aiohttp
BASE = "http://127.0.0.1:8001"
TOKEN = open("/creds/owner.token").read().strip()
H = {"Authorization": "Bearer " + TOKEN}
SKIP = {"audit_append", "user_preferences", "system_config", "rote_config", "chrome_render", "chrome_surface",
        "history_list", "chat_history", "llm_usage_report", "conversation_snapshot", "user_message_acked"}

async def rest(s, method, path, headers=H, **kw):
    async with s.request(method, BASE + path, headers=headers, **kw) as r:
        text = await r.text()
        print(f"{method} {path} -> {r.status} {text[:160]}", flush=True)
        return r.status, (json.loads(text) if text[:1] in "{[" else text)

async def pump(ws, seconds, stop=None):
    loop = asyncio.get_event_loop(); end = loop.time() + seconds
    while loop.time() < end:
        try:
            msg = await ws.receive(timeout=end - loop.time())
        except asyncio.TimeoutError:
            return
        if msg.type != aiohttp.WSMsgType.TEXT:
            return
        f = json.loads(msg.data)
        yield f
        if stop and stop(f):
            return

def ui(action, session, **payload):
    return {"type": "ui_event", "action": action, "session_id": session,
            "payload": {"submission_id": str(uuid.uuid4()), "request_generation": str(uuid.uuid4()), **payload}}

async def main():
    async with aiohttp.ClientSession() as s:
        await rest(s, "GET", "/healthz", headers={})
        await rest(s, "GET", "/api/chats", headers={})
        await rest(s, "GET", "/api/chats", headers={"Authorization": "Bearer " + TOKEN[:-6] + "AAAAAA"})
        await rest(s, "GET", "/api/chats")
        _, chat = await rest(s, "POST", "/api/chats", json={})
        chat_id = chat["chat_id"]
        await rest(s, "PUT", "/api/agents/general-1/permissions", json={"scopes": {"tools:read": True}})
        async with s.ws_connect(BASE + "/ws") as ws:
            await ws.send_json({"type": "register_ui", "token": TOKEN, "capabilities": [], "device": {"device_type": "browser"}})
            async for f in pump(ws, 5):
                pass
            fields = {"provider": "custom", "base_url": "http://sn105-model:8080/v1", "model": "fake-model",
                      "api_key": "test-key-local-fake", "data_sharing_acknowledged": True}
            await ws.send_json(ui("chrome_llm_save", chat_id, surface="llm", fields=fields))
            async for f in pump(ws, 30, stop=lambda f: f.get("type") == "llm_config_ack"):
                if f.get("type") == "llm_config_ack":
                    print("LLM-CONFIG", json.dumps(f), flush=True)
            await asyncio.sleep(3)
            await ws.send_json(ui("chat_message", chat_id, message="Please chart these two values: a is 1 and b is 2.", async_mode=False))
            async for f in pump(ws, 90, stop=lambda f: f.get("type") == "operation_status" and f.get("state") in ("completed", "failed")):
                t = f.get("type")
                if t in SKIP:
                    continue
                brief = {k: f[k] for k in ("state", "status", "message", "action", "tool_name", "code") if k in f}
                if t == "ui_render":
                    brief["components"] = json.dumps(f.get("components"))[:200]
                print("FRAME", t, json.dumps(brief), flush=True)
        _, detail = await rest(s, "GET", f"/api/chats/{chat_id}")
        for m in detail["chat"]["messages"]:
            print("MSG", m.get("role"), json.dumps(m)[:260])

asyncio.run(main())
