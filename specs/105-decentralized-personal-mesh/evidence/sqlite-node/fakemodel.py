"""Deterministic OpenAI-compatible model: calls the named tool once, then answers from the tool result."""
import json, os, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TARGET = os.environ.get("FAKE_TOOL", "")
FIXED_ARGS = os.environ.get("FAKE_ARGS", "")


def log(*parts):
    sys.stderr.write(" ".join(str(p) for p in parts) + "\n"); sys.stderr.flush()


def tool_args(spec):
    params = (spec.get("function") or {}).get("parameters") or {}
    out = {}
    for name in params.get("required") or []:
        kind = ((params.get("properties") or {}).get(name) or {}).get("type")
        out[name] = {"integer": 1, "number": 1, "boolean": True, "array": [], "object": {}}.get(kind, "x")
    return out


def completion(body):
    messages = body.get("messages") or []
    tools = body.get("tools") or []
    names = [(t.get("function") or {}).get("name") for t in tools]
    log("REQUEST roles=", [m.get("role") for m in messages], "tools=", len(tools), "stream=", bool(body.get("stream")))
    used = any(m.get("role") == "tool" for m in messages)
    if tools and not used and body.get("tool_choice") != "none":
        pick = next((t for t in tools if ((t.get("function") or {}).get("name") or "").endswith(TARGET)), None)
        if pick:
            call = {"id": "call_fake_1", "type": "function",
                    "function": {"name": pick["function"]["name"], "arguments": FIXED_ARGS or json.dumps(tool_args(pick))}}
            log("TOOLCALL", TARGET)
            return {"content": None, "tool_calls": [call], "finish": "tool_calls"}
        log("TARGET NOT OFFERED; offered:", sorted(n for n in names if n)[:40])
    tool_text = next((m.get("content") for m in reversed(messages) if m.get("role") == "tool"), None)
    text = "FAKE-MODEL-OK" + (": tool result received (%d chars)" % len(str(tool_text)) if tool_text is not None else "")
    return {"content": text, "tool_calls": None, "finish": "stop"}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, payload, ctype="application/json"):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status); self.send_header("content-type", ctype); self.send_header("content-length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._send(200, {"object": "list", "data": [{"id": "fake-model", "object": "model", "owned_by": "local"}]})
        else:
            self._send(200, {"ok": True})

    def do_POST(self):
        size = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(size) or b"{}")
        result = completion(body)
        message = {"role": "assistant", "content": result["content"]}
        if result["tool_calls"]:
            message["tool_calls"] = result["tool_calls"]
        base = {"id": "chatcmpl-fake", "object": "chat.completion", "created": int(time.time()), "model": "fake-model"}
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        if not body.get("stream"):
            self._send(200, base | {"choices": [{"index": 0, "message": message, "finish_reason": result["finish"]}], "usage": usage})
            return
        delta = {"role": "assistant"}
        if result["content"]:
            delta["content"] = result["content"]
        if result["tool_calls"]:
            delta["tool_calls"] = [dict(c, index=0) for c in result["tool_calls"]]
        chunk = base | {"object": "chat.completion.chunk"}
        events = [chunk | {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                  chunk | {"choices": [{"index": 0, "delta": {}, "finish_reason": result["finish"]}]},
                  chunk | {"choices": [], "usage": usage}]
        raw = "".join("data: %s\n\n" % json.dumps(e) for e in events) + "data: [DONE]\n\n"
        self._send(200, raw.encode(), "text/event-stream")

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
