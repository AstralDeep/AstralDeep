"""An OpenAI-compatible chat server that answers with scripted tool calls and then a text reply, recording every request body and authorization header.
It generalises the fake model in `test_node_mesh_peer_tools.py` so connection and peer tests can share one implementation, and each user message restarts its script so a later turn in the same chat replays it from the top.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field

from .loopback import Loopback, Request, Response


@dataclass
class ScriptedTurn:
    text: str | None = None
    tool: str | None = None
    arguments: dict = field(default_factory=dict)


class ModelServer:
    def __init__(
        self,
        tool: str | None = None,
        arguments: dict | None = None,
        reply: str = "done",
        script: list[ScriptedTurn] | None = None,
        tls: tuple[str, str] | None = None,
    ) -> None:
        self.script = script or ([ScriptedTurn(tool=tool, arguments=arguments or {})] if tool else []) + [ScriptedTurn(text=reply)]
        self.requests: list[dict] = []
        self.authorizations: list[str | None] = []
        self._lock = threading.Lock()
        self._server = Loopback(self._handle, tls)
        self.url = self._server.url

    def stop(self) -> None:
        self._server.stop()

    def __enter__(self) -> ModelServer:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def _turn(self, request: dict) -> ScriptedTurn:
        answered = 0
        for message in request.get("messages", []):
            if message.get("role") == "user":
                answered = 0
            elif message.get("role") == "tool":
                answered += 1
        return self.script[min(answered, len(self.script) - 1)]

    def _handle(self, req: Request) -> Response:
        body = req.json()
        with self._lock:
            index = len(self.requests)
            self.requests.append(body)
            self.authorizations.append(req.headers.get("authorization"))
        turn = self._turn(body)
        if turn.tool is None:
            deltas, finish = [{"content": turn.text or ""}], "stop"
        else:
            call = {
                "index": 0,
                "id": f"call-{index + 1}",
                "type": "function",
                "function": {"name": turn.tool, "arguments": json.dumps(turn.arguments)},
            }
            deltas, finish = [{"tool_calls": [call]}], "tool_calls"
        usage = {"prompt_tokens": 12, "completion_tokens": 6}
        stream = "".join(f"data: {json.dumps({'choices': [{'delta': delta}]})}\n\n" for delta in deltas)
        stream += f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': finish}], 'usage': usage})}\n\n"
        stream += "data: [DONE]\n\n"
        return Response(200, [("content-type", "text/event-stream")], stream.encode())
