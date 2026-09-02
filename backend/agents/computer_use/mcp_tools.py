"""computer-use-1 verb library (feature 076, spec contracts/verbs.md).

Every verb runs in the agent's worker thread (``process_request`` is called via
``asyncio.to_thread``) and bridges to the orchestrator loop with
``run_coroutine_threadsafe`` to push a ``computer_request`` to the owner's host
and await the correlated ``computer_response``. The registry declares scope,
tier, timeout and the SAME destructive classification object the dispatch gate
reads (``orchestrator.computer_use_policy``) so verb + gate cannot drift.

Result contract (two tiers): ``_data`` is the small typed digest the model reads;
``_ui_components`` is renderer-only; ``_images`` (screenshot only) becomes image
parts for the model. Typed failures are Alerts with ``variant="error"`` — the
MCP server turns them into error responses whose message names the code and
the next action, so the model can recover and never sees a traceback.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from astralprims import Alert, Card, Image, Table, Text

from orchestrator.computer_hosts import ComputerHostError
from orchestrator.computer_use_policy import (
    DEFAULT_READ_BYTES,
    DEFAULT_SCREENSHOT_WIDTH,
    DESTRUCTIVE_CLASSIFICATION,
    MAX_CLIPBOARD_CHARS,
    MAX_READ_BYTES,
    MAX_SCREENSHOT_WIDTH,
    MAX_SCROLL_NOTCHES,
    MAX_SUMMARY_CHARS,
    MAX_TEXT_CHARS,
    MAX_WAIT_SECONDS,
    MAX_WRITE_BYTES,
    MIN_SCREENSHOT_WIDTH,
    SCOPES,
    TIERS,
    TIMEOUTS,
)

logger = logging.getLogger("ComputerUseTools")

_ORCH = None
_LOOP = None
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_KEY_CHORD = re.compile(r"^[a-z0-9+_\- ]{1,64}$", re.IGNORECASE)
_APP_NAME = re.compile(r"^[\w .+\-]{1,80}$")
#: A launchable path: a drive-rooted or UNC Windows path made of safe characters
#: (no shell metacharacters, no wildcards, no redirection).
_APP_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)[\w \\/:.\-()+~]{1,1000}$")


def register_deps(orchestrator) -> None:
    """Bind the in-process orchestrator (host registry + session manager) and
    remember its event loop when we are called from inside it (boot)."""
    global _ORCH, _LOOP
    _ORCH = orchestrator
    try:
        _LOOP = asyncio.get_running_loop()
    except RuntimeError:
        _LOOP = None


def _loop(kwargs: Dict[str, Any]):
    """The orchestrator loop to bridge into: the per-request runtime's loop
    (always the orchestrator's for an in-process agent), else the boot loop."""
    runtime = kwargs.get("_runtime")
    return getattr(runtime, "loop", None) or _LOOP


def _registry():
    return getattr(_ORCH, "computer_hosts", None)


def _sessions():
    return getattr(_ORCH, "computer_sessions", None)


# ── result helpers ────────────────────────────────────────────────────────────

def _sanitize(value: Any, limit: int = 500) -> str:
    if value is None:
        return ""
    text = _CTRL.sub("", str(value))
    return text if len(text) <= limit else text[:limit] + "…"


def _data(data: Dict[str, Any], components: Optional[List[Any]] = None) -> Dict[str, Any]:
    return {"_ui_components": [c.to_dict() for c in (components or [])], "_data": data}


def _fail(code: str, message: str, *, next_action: str = "", **extra) -> Dict[str, Any]:
    text = f"{code}: {_sanitize(message, 400)}" + (f" — {next_action}" if next_action else "")
    return {"_ui_components": [Alert(message=text, variant="error").to_dict()],
            "_data": {"code": code, "message": message, "next_action": next_action, **extra}}


def _host_error(exc: ComputerHostError) -> Dict[str, Any]:
    extra = {"candidates": exc.candidates} if exc.candidates else {}
    hint = ""
    if exc.code == "ambiguous_computer" and exc.candidates:
        hint = "pass computer=<one of " + ", ".join(exc.candidates[:6]) + ">"
    elif exc.code == "no_session":
        hint = "call start_session first"
    elif exc.code == "paused":
        hint = "someone is using the computer; call resume_session when it is free"
    return _fail(exc.code, exc.message, next_action=hint, **extra)


# ── context resolution ────────────────────────────────────────────────────────

def _bridge(coro, timeout: float, loop=None):
    """Run an orchestrator coroutine from the worker thread."""
    loop = loop or _LOOP
    if loop is None:
        coro.close()
        raise ComputerHostError("failed", "orchestrator loop unavailable")
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout + 5.0)


def _resolve_host(user_id: str, chat_id: Optional[str], ref: Optional[str]):
    """The host a verb targets: an explicit reference wins; otherwise the live
    session bound to this chat; otherwise the owner's single online host."""
    registry, sessions = _registry(), _sessions()
    if registry is None or sessions is None:
        raise ComputerHostError("computer_unavailable", "remote control is not enabled on this server")
    if ref is None or str(ref).strip() == "":
        session = sessions.live_for_chat(user_id, chat_id)
        if session is None:
            live = sessions.live_for_owner(user_id)
            if len(live) == 1:
                session = live[0]
        if session is not None:
            host = registry.get(user_id, session.host_id)
            if host is not None:
                return host
    return registry.resolve(user_id, ref)


def _require_session(user_id: str, host) -> Tuple[Any, Optional[Dict[str, Any]]]:
    session = _sessions().live_for_host(user_id, host.host_id)
    if session is None:
        return None, _fail("no_session", f"no remote-control session on {host.name}",
                           next_action="call start_session first")
    if session.state == "paused":
        return None, _fail("paused", f"{host.name} is paused ({session.pause_reason or 'someone is using it'})",
                           next_action="wait, then call resume_session")
    return session, None


def _ctx(kwargs: Dict[str, Any]):
    """Common preamble for HOST verbs: (user_id, chat_id, host, session) or a
    failure dict in the fourth slot."""
    user_id = kwargs.get("user_id")
    if not user_id:
        return None, None, None, _fail("unattended_refused", "sign in to control your computer")
    chat_id = kwargs.get("session_id")
    try:
        host = _resolve_host(user_id, chat_id, kwargs.get("computer"))
    except ComputerHostError as exc:
        return user_id, chat_id, None, _host_error(exc)
    session, err = _require_session(user_id, host)
    if err:
        return user_id, chat_id, host, err
    return user_id, chat_id, host, session


def _run(host, session, verb: str, args: Dict[str, Any], loop=None):
    """Push the request through the session lock and return the raw result or
    a failure dict."""
    registry, sessions = _registry(), _sessions()
    timeout = TIMEOUTS[verb]

    async def _go():
        async with session.lock:
            if session.state != "active":
                raise ComputerHostError("paused" if session.state == "paused" else "no_session",
                                        f"session is {session.state}")
            result = await registry.request(host, session.session_id, verb, args, timeout)
            session.verbs_run += 1
            sessions.touch(session)
            return result

    try:
        return _bridge(_go(), timeout, loop), None
    except ComputerHostError as exc:
        return None, _host_error(exc)
    except Exception as exc:  # noqa: BLE001 — typed, never a traceback into chat
        logger.warning("076: %s failed: %s", verb, exc)
        return None, _fail("failed", f"{verb} failed: {exc}")


def _int(value: Any, name: str, lo: int, hi: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer")
    if not (lo <= v <= hi):
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return v


def _xy(kwargs: Dict[str, Any], xk: str = "x", yk: str = "y") -> Tuple[int, int]:
    return _int(kwargs.get(xk), xk, 0, 32767), _int(kwargs.get(yk), yk, 0, 32767)


# ── session verbs ─────────────────────────────────────────────────────────────

def list_computers(**kwargs) -> Dict[str, Any]:
    user_id = kwargs.get("user_id")
    if not user_id:
        return _fail("unattended_refused", "sign in to see your computers")
    registry, sessions = _registry(), _sessions()
    if registry is None:
        return _fail("computer_unavailable", "remote control is not enabled on this server")
    rows = registry.list_for_owner(user_id)
    live = {s.host_id: s.public() for s in sessions.live_for_owner(user_id)}
    out = []
    for r in rows:
        s = live.get(r["host_id"])
        out.append({**r, "session": {"state": s["state"], "controller": s["controller_label"],
                                     "session_id": s["session_id"]} if s else None})
    if not out:
        return _data({"computers": [], "note": "no computers — on the PC open the AstralDeep "
                                                "desktop client and switch on Settings → Remote control"},
                     [Card(title="My computers", content=[Text(
                         content="No computers yet. On your PC, open the AstralDeep desktop client "
                                 "and switch on Settings → Remote control.", variant="body")])])
    table = Table(headers=["Computer", "Platform", "Status", "Session"],
                  rows=[[r["name"], r["platform"], "online" if r["online"] else "offline",
                         (r["session"]["state"] + " · " + r["session"]["controller"]) if r["session"] else "—"]
                        for r in out])
    return _data({"computers": out}, [Card(title="My computers", content=[table])])


def start_session(**kwargs) -> Dict[str, Any]:
    user_id = kwargs.get("user_id")
    if not user_id:
        return _fail("unattended_refused", "sign in to control your computer")
    chat_id = kwargs.get("session_id")
    registry, sessions = _registry(), _sessions()
    if registry is None or sessions is None:
        return _fail("computer_unavailable", "remote control is not enabled on this server")
    try:
        host = registry.resolve(user_id, kwargs.get("computer"))
    except ComputerHostError as exc:
        return _host_error(exc)
    websocket = _ORCH.socket_for_chat(user_id, chat_id) if hasattr(_ORCH, "socket_for_chat") else None
    if websocket is None:
        return _fail("unattended_refused", "a remote-control session needs a live person on an interactive client")
    try:
        session = _bridge(sessions.start(user_id, host, websocket, chat_id), TIMEOUTS["start_session"], _loop(kwargs))
    except ComputerHostError as exc:
        return _host_error(exc)
    except Exception as exc:  # noqa: BLE001
        return _fail("failed", f"could not start the session: {exc}")
    vision = "yes" if session.images_supported else "no — non-visual mode"
    data = {"session_id": session.session_id, "host_id": host.host_id, "computer": host.name,
            "platform": host.platform, "screens": host.screens, "state": session.state,
            "images_supported": session.images_supported,
            "note": "Take a screenshot to see the screen; coordinates you send are in the "
                    "screenshot's pixel space."}
    card = Card(title=f"Controlling {host.name}", content=[
        Text(content=f"Session started from your {session.controller_label}. "
                     f"Screens: {len(host.screens)} · vision: {vision}.", variant="body"),
        Text(content="The computer shows a banner while this is active; anyone at it can pause "
                     "or stop. Commands and file changes will ask you to approve.", variant="caption"),
    ])
    return _data(data, [card])


def end_session(**kwargs) -> Dict[str, Any]:
    user_id = kwargs.get("user_id")
    if not user_id:
        return _fail("unattended_refused", "sign in first")
    chat_id = kwargs.get("session_id")
    try:
        host = _resolve_host(user_id, chat_id, kwargs.get("computer"))
    except ComputerHostError as exc:
        return _host_error(exc)
    session = _sessions().live_for_host(user_id, host.host_id)
    if session is None:
        return _data({"ended": False, "note": f"no session on {host.name}"})
    _bridge(_sessions().end(session, "user_stop"), TIMEOUTS["end_session"], _loop(kwargs))
    return _data({"ended": True, "reason": "user_stop", "computer": host.name, "verbs_run": session.verbs_run},
                 [Text(content=f"Stopped controlling {host.name}.", variant="body")])


def resume_session(**kwargs) -> Dict[str, Any]:
    user_id = kwargs.get("user_id")
    if not user_id:
        return _fail("unattended_refused", "sign in first")
    chat_id = kwargs.get("session_id")
    try:
        host = _resolve_host(user_id, chat_id, kwargs.get("computer"))
    except ComputerHostError as exc:
        return _host_error(exc)
    session = _sessions().live_for_host(user_id, host.host_id)
    if session is None:
        return _fail("no_session", f"no session on {host.name}", next_action="call start_session")
    _bridge(_sessions().resume(session), TIMEOUTS["resume_session"], _loop(kwargs))
    return _data({"state": session.state, "computer": host.name})


def confirm_action(**kwargs) -> Dict[str, Any]:
    """Reached only AFTER the owner approved the proposal card (the gate refuses
    the first reach with the card). Returning approved lets the model continue."""
    summary = _sanitize(kwargs.get("summary"), MAX_SUMMARY_CHARS)
    return _data({"approved": True, "summary": summary},
                 [Text(content=f"Approved: {summary}", variant="caption")])


# ── observe verbs ─────────────────────────────────────────────────────────────

def screenshot(**kwargs) -> Dict[str, Any]:
    user_id, chat_id, host, ctx = _ctx(kwargs)
    if not hasattr(ctx, "session_id"):
        return ctx
    session = ctx
    try:
        screen_index = _int(kwargs.get("screen_index", 0), "screen_index", 0, max(0, len(host.screens) - 1))
        max_width = _int(kwargs.get("max_width", DEFAULT_SCREENSHOT_WIDTH), "max_width",
                         MIN_SCREENSHOT_WIDTH, MAX_SCREENSHOT_WIDTH)
    except ValueError as exc:
        return _fail("out_of_range", str(exc))
    result, err = _run(host, session, "screenshot", {"screen_index": screen_index, "max_width": max_width}, _loop(kwargs))
    if err:
        return err
    b64 = str(result.get("base64") or "")
    media_type = str(result.get("media_type") or "image/jpeg")
    if not b64 or media_type not in ("image/jpeg", "image/png", "image/webp"):
        return _fail("failed", f"{host.name} returned no image")
    width, height = int(result.get("width") or 0), int(result.get("height") or 0)
    scale = float(result.get("scale") or 1.0)
    session.last_screenshot = {"width": width, "height": height, "scale": scale,
                               "screen_index": screen_index, "at": time.time()}
    caption = f"Screenshot of {host.name} — screen {screen_index}, {width}×{height} px"
    image = Image(id=f"au_cuview_{session.session_id}", url=f"data:{media_type};base64,{b64}",
                  alt=caption, width="100%")
    data = {"computer": host.name, "screen_index": screen_index, "width": width, "height": height,
            "scale": scale, "note": "the screenshot is attached as an image; click/drag "
                                    "coordinates are in its pixel space (0,0 = top-left)"}
    if not session.images_supported:
        data["note"] = ("your model cannot see images — use list_windows, focus_window, type_text "
                        "and press_keys to work without the picture")
    return {"_ui_components": [image.to_dict()], "_data": data,
            "_images": ([{"media_type": media_type, "base64": b64, "caption": caption}]
                        if session.images_supported else [])}


def list_windows(**kwargs) -> Dict[str, Any]:
    _u, _c, host, ctx = _ctx(kwargs)
    if not hasattr(ctx, "session_id"):
        return ctx
    result, err = _run(host, ctx, "list_windows", {}, _loop(kwargs))
    if err:
        return err
    windows = []
    for w in (result.get("windows") or [])[:100]:
        if isinstance(w, dict):
            windows.append({"hwnd": int(w.get("hwnd") or 0), "title": _sanitize(w.get("title"), 200),
                            "process": _sanitize(w.get("process"), 80), "rect": w.get("rect"),
                            "focused": bool(w.get("focused")), "minimized": bool(w.get("minimized"))})
    return _data({"computer": host.name, "windows": windows, "count": len(windows)})


def get_clipboard(**kwargs) -> Dict[str, Any]:
    _u, _c, host, ctx = _ctx(kwargs)
    if not hasattr(ctx, "session_id"):
        return ctx
    result, err = _run(host, ctx, "get_clipboard", {}, _loop(kwargs))
    if err:
        return err
    text = _sanitize(result.get("text"), MAX_CLIPBOARD_CHARS)
    return _data({"computer": host.name, "text": text, "truncated": bool(result.get("truncated"))})


def read_file(**kwargs) -> Dict[str, Any]:
    _u, _c, host, ctx = _ctx(kwargs)
    if not hasattr(ctx, "session_id"):
        return ctx
    path = str(kwargs.get("path") or "").strip()
    if not path or "\x00" in path or len(path) > 1024:
        return _fail("out_of_range", "path must be a non-empty absolute path")
    try:
        max_bytes = _int(kwargs.get("max_bytes", DEFAULT_READ_BYTES), "max_bytes", 1, MAX_READ_BYTES)
    except ValueError as exc:
        return _fail("out_of_range", str(exc))
    result, err = _run(host, ctx, "read_file", {"path": path, "max_bytes": max_bytes}, _loop(kwargs))
    if err:
        return err
    return _data({"computer": host.name, "path": path, "text": _sanitize(result.get("text"), max_bytes),
                  "truncated": bool(result.get("truncated")), "size": int(result.get("size") or 0)})


def list_dir(**kwargs) -> Dict[str, Any]:
    _u, _c, host, ctx = _ctx(kwargs)
    if not hasattr(ctx, "session_id"):
        return ctx
    path = str(kwargs.get("path") or "").strip()
    if not path or "\x00" in path or len(path) > 1024:
        return _fail("out_of_range", "path must be a non-empty absolute path")
    result, err = _run(host, ctx, "list_dir", {"path": path}, _loop(kwargs))
    if err:
        return err
    entries = []
    for e in (result.get("entries") or [])[:500]:
        if isinstance(e, dict):
            entries.append({"name": _sanitize(e.get("name"), 255), "is_dir": bool(e.get("is_dir")),
                            "size": int(e.get("size") or 0), "modified": e.get("modified")})
    return _data({"computer": host.name, "path": path, "entries": entries, "count": len(entries)})


def wait(**kwargs) -> Dict[str, Any]:
    _u, _c, host, ctx = _ctx(kwargs)
    if not hasattr(ctx, "session_id"):
        return ctx
    try:
        seconds = float(kwargs.get("seconds", 1.0))
    except (TypeError, ValueError):
        return _fail("out_of_range", "seconds must be a number")
    if not (0.1 <= seconds <= MAX_WAIT_SECONDS):
        return _fail("out_of_range", f"seconds must be between 0.1 and {MAX_WAIT_SECONDS}")
    result, err = _run(host, ctx, "wait", {"seconds": seconds}, _loop(kwargs))
    if err:
        return err
    return _data({"computer": host.name, "waited": seconds})


# ── input verbs ───────────────────────────────────────────────────────────────

def _pointer(verb: str, kwargs: Dict[str, Any], extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    _u, _c, host, ctx = _ctx(kwargs)
    if not hasattr(ctx, "session_id"):
        return ctx
    try:
        x, y = _xy(kwargs)
    except ValueError as exc:
        return _fail("out_of_range", str(exc))
    args = {"x": x, "y": y, **(extra or {})}
    result, err = _run(host, ctx, verb, args, _loop(kwargs))
    if err:
        return err
    return _data({"computer": host.name, **args, **({k: v for k, v in result.items() if k in ("x", "y")})})


def click(**kwargs) -> Dict[str, Any]:
    button = str(kwargs.get("button") or "left").lower()
    if button not in ("left", "right", "middle"):
        return _fail("out_of_range", "button must be left, right or middle")
    try:
        count = _int(kwargs.get("count", 1), "count", 1, 2)
    except ValueError as exc:
        return _fail("out_of_range", str(exc))
    return _pointer("click", kwargs, {"button": button, "count": count})


def double_click(**kwargs) -> Dict[str, Any]:
    return _pointer("double_click", kwargs)


def right_click(**kwargs) -> Dict[str, Any]:
    return _pointer("right_click", kwargs)


def move(**kwargs) -> Dict[str, Any]:
    return _pointer("move", kwargs)


def drag(**kwargs) -> Dict[str, Any]:
    _u, _c, host, ctx = _ctx(kwargs)
    if not hasattr(ctx, "session_id"):
        return ctx
    try:
        x1, y1 = _xy(kwargs, "x1", "y1")
        x2, y2 = _xy(kwargs, "x2", "y2")
    except ValueError as exc:
        return _fail("out_of_range", str(exc))
    result, err = _run(host, ctx, "drag", {"x1": x1, "y1": y1, "x2": x2, "y2": y2}, _loop(kwargs))
    if err:
        return err
    return _data({"computer": host.name, "x1": x1, "y1": y1, "x2": x2, "y2": y2})


def scroll(**kwargs) -> Dict[str, Any]:
    try:
        dx = _int(kwargs.get("dx", 0), "dx", -MAX_SCROLL_NOTCHES, MAX_SCROLL_NOTCHES)
        dy = _int(kwargs.get("dy", -3), "dy", -MAX_SCROLL_NOTCHES, MAX_SCROLL_NOTCHES)
    except ValueError as exc:
        return _fail("out_of_range", str(exc))
    return _pointer("scroll", kwargs, {"dx": dx, "dy": dy})


def type_text(**kwargs) -> Dict[str, Any]:
    _u, _c, host, ctx = _ctx(kwargs)
    if not hasattr(ctx, "session_id"):
        return ctx
    text = kwargs.get("text")
    if not isinstance(text, str) or not text:
        return _fail("out_of_range", "text must be a non-empty string")
    if len(text) > MAX_TEXT_CHARS:
        return _fail("out_of_range", f"text is limited to {MAX_TEXT_CHARS} characters per call")
    result, err = _run(host, ctx, "type_text", {"text": text, "terminal_ok": ctx.terminal_ok}, _loop(kwargs))
    if err:
        return err
    return _data({"computer": host.name, "chars": len(text)})


def press_keys(**kwargs) -> Dict[str, Any]:
    _u, _c, host, ctx = _ctx(kwargs)
    if not hasattr(ctx, "session_id"):
        return ctx
    keys = str(kwargs.get("keys") or "").strip().lower()
    if not keys or _KEY_CHORD.fullmatch(keys) is None:
        return _fail("out_of_range", "keys must be a chord like 'ctrl+s', 'enter' or 'alt+f4'")
    result, err = _run(host, ctx, "press_keys", {"keys": keys, "terminal_ok": ctx.terminal_ok}, _loop(kwargs))
    if err:
        return err
    return _data({"computer": host.name, "keys": keys})


def focus_window(**kwargs) -> Dict[str, Any]:
    _u, _c, host, ctx = _ctx(kwargs)
    if not hasattr(ctx, "session_id"):
        return ctx
    args: Dict[str, Any] = {}
    if kwargs.get("hwnd") is not None:
        try:
            args["hwnd"] = _int(kwargs.get("hwnd"), "hwnd", 1, 2**53)
        except ValueError as exc:
            return _fail("out_of_range", str(exc))
    title = kwargs.get("title")
    if isinstance(title, str) and title.strip():
        args["title"] = _sanitize(title.strip(), 200)
    if not args:
        return _fail("out_of_range", "give a window hwnd (from list_windows) or a title substring")
    result, err = _run(host, ctx, "focus_window", args, _loop(kwargs))
    if err:
        return err
    return _data({"computer": host.name, "hwnd": int(result.get("hwnd") or 0),
                  "title": _sanitize(result.get("title"), 200)})


def open_app(**kwargs) -> Dict[str, Any]:
    _u, _c, host, ctx = _ctx(kwargs)
    if not hasattr(ctx, "session_id"):
        return ctx
    app = str(kwargs.get("app") or "").strip()
    if (not app or len(app) > 1024 or "\x00" in app
            or (_APP_NAME.fullmatch(app) is None and _APP_PATH.fullmatch(app) is None)):
        return _fail("out_of_range", "app must be a bare application name (e.g. notepad, excel) "
                                     "or the full path of an .exe/.lnk")
    args_list = kwargs.get("args") or []
    if not isinstance(args_list, list) or any(not isinstance(a, str) or len(a) > 512 for a in args_list) \
            or len(args_list) > 16:
        return _fail("out_of_range", "args must be a short list of strings")
    result, err = _run(host, ctx, "open_app", {"app": app, "args": args_list}, _loop(kwargs))
    if err:
        return err
    return _data({"computer": host.name, "app": app, "launched": bool(result.get("launched", True)),
                  "pid": result.get("pid")})


def set_clipboard(**kwargs) -> Dict[str, Any]:
    _u, _c, host, ctx = _ctx(kwargs)
    if not hasattr(ctx, "session_id"):
        return ctx
    text = kwargs.get("text")
    if not isinstance(text, str) or len(text) > MAX_CLIPBOARD_CHARS:
        return _fail("out_of_range", f"text must be a string of at most {MAX_CLIPBOARD_CHARS} characters")
    result, err = _run(host, ctx, "set_clipboard", {"text": text}, _loop(kwargs))
    if err:
        return err
    return _data({"computer": host.name, "chars": len(text)})


# ── consequential verbs (gated on every reach by the confirmation mechanism) ─

def write_file(**kwargs) -> Dict[str, Any]:
    _u, _c, host, ctx = _ctx(kwargs)
    if not hasattr(ctx, "session_id"):
        return ctx
    path = str(kwargs.get("path") or "").strip()
    content = kwargs.get("content")
    if not path or "\x00" in path or len(path) > 1024:
        return _fail("out_of_range", "path must be a non-empty absolute path")
    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        return _fail("out_of_range", f"content must be text of at most {MAX_WRITE_BYTES} bytes")
    if_exists = str(kwargs.get("if_exists") or "refuse")
    if if_exists not in ("refuse", "overwrite"):
        return _fail("out_of_range", "if_exists must be refuse or overwrite")
    result, err = _run(host, ctx, "write_file", {"path": path, "content": content, "if_exists": if_exists}, _loop(kwargs))
    if err:
        return err
    return _data({"computer": host.name, "path": path, "bytes": int(result.get("bytes") or 0)})


def delete_path(**kwargs) -> Dict[str, Any]:
    _u, _c, host, ctx = _ctx(kwargs)
    if not hasattr(ctx, "session_id"):
        return ctx
    path = str(kwargs.get("path") or "").strip()
    if not path or "\x00" in path or len(path) > 1024:
        return _fail("out_of_range", "path must be a non-empty absolute path")
    result, err = _run(host, ctx, "delete_path", {"path": path}, _loop(kwargs))
    if err:
        return err
    return _data({"computer": host.name, "path": path, "deleted": bool(result.get("deleted"))})


# ── registry ──────────────────────────────────────────────────────────────────

_COMPUTER = {"computer": {"type": "string",
                          "description": "Which computer (name or id from list_computers). Optional "
                                         "when a session is active or only one computer is online."}}


def _entry(fn, description: str, properties: Dict[str, Any], required: Optional[List[str]] = None):
    name = fn.__name__
    entry = {
        "function": fn,
        "description": description,
        "input_schema": {"type": "object", "properties": {**_COMPUTER, **properties},
                         "required": list(required or [])},
        "scope": SCOPES[name],
        "tier": TIERS[name],
        "retryable": False,
        "timeout": TIMEOUTS[name],
    }
    if name in DESTRUCTIVE_CLASSIFICATION:
        entry["destructive"] = DESTRUCTIVE_CLASSIFICATION[name]
    return entry


_XY = {"x": {"type": "integer", "description": "X in the last screenshot's pixel space"},
       "y": {"type": "integer", "description": "Y in the last screenshot's pixel space"}}

TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "list_computers": {
        **_entry(list_computers, "List the user's computers that run the AstralDeep desktop client "
                                 "with remote control switched on, with online status and any active session.", {}),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    "start_session": _entry(
        start_session,
        "Start (or re-join) a remote-control session on one of the user's computers. Required before "
        "any screenshot or action. The computer shows a banner while the session is active.", {}),
    "end_session": _entry(end_session, "End the remote-control session on a computer.", {}),
    "resume_session": _entry(resume_session, "Resume a session that was paused because someone used the "
                                             "computer locally.", {}),
    "confirm_action": _entry(
        confirm_action,
        "Ask the user to approve a consequential step you are about to take in the UI — buying, paying, "
        "sending a message or email, signing in, deleting, running a command in a terminal, or anything "
        "hard to undo. Call it BEFORE the step (the task resumes automatically after they tap Approve); "
        "an approval also unlocks typing into a terminal for a few minutes.",
        {"summary": {"type": "string", "description": "One sentence: what you are about to do and why."}},
        ["summary"]),
    "screenshot": _entry(
        screenshot,
        "Take a screenshot of the computer's screen. You receive it as an image; the reply's width/height "
        "define the coordinate space for click/move/drag/scroll. Take one after every action that "
        "changes the screen.",
        {"screen_index": {"type": "integer", "description": "Which screen (0 = primary)."},
         "max_width": {"type": "integer", "description": "Downscale to this width (320-1920, default 1280)."}}),
    "list_windows": _entry(list_windows, "List the open top-level windows (title, process, position, "
                                         "which is focused). Works without vision.", {}),
    "get_clipboard": _entry(get_clipboard, "Read the computer's clipboard text.", {}),
    "read_file": _entry(read_file, "Read a text file on the computer (bounded).",
                        {"path": {"type": "string", "description": "Absolute path."},
                         "max_bytes": {"type": "integer", "description": "Default 65536, max 262144."}},
                        ["path"]),
    "list_dir": _entry(list_dir, "List a directory on the computer.",
                       {"path": {"type": "string", "description": "Absolute directory path."}}, ["path"]),
    "wait": _entry(wait, "Wait for the screen to settle (0.1-10 s) before the next screenshot.",
                   {"seconds": {"type": "number"}}),
    "click": _entry(click, "Click at a point of the last screenshot.",
                    {**_XY, "button": {"type": "string", "enum": ["left", "right", "middle"]},
                     "count": {"type": "integer", "description": "1 or 2."}}, ["x", "y"]),
    "double_click": _entry(double_click, "Double-click at a point.", _XY, ["x", "y"]),
    "right_click": _entry(right_click, "Right-click at a point.", _XY, ["x", "y"]),
    "move": _entry(move, "Move the mouse pointer to a point (hover).", _XY, ["x", "y"]),
    "drag": _entry(drag, "Press at (x1,y1), drag to (x2,y2), release.",
                   {"x1": {"type": "integer"}, "y1": {"type": "integer"},
                    "x2": {"type": "integer"}, "y2": {"type": "integer"}}, ["x1", "y1", "x2", "y2"]),
    "scroll": _entry(scroll, "Scroll at a point. dy < 0 scrolls down (default -3 notches), dy > 0 up.",
                     {**_XY, "dx": {"type": "integer"}, "dy": {"type": "integer"}}, ["x", "y"]),
    "type_text": _entry(type_text, "Type text into the focused control (Unicode; up to 4000 characters). "
                                   "Typing into a terminal/console needs the user's approval: if it is "
                                   "refused with confirmation_required, call confirm_action describing the "
                                   "command, wait for approval, then type again.",
                        {"text": {"type": "string"}}, ["text"]),
    "press_keys": _entry(press_keys, "Press a key or chord: 'enter', 'tab', 'escape', 'ctrl+s', "
                                     "'ctrl+shift+t', 'alt+f4', 'win+r'.",
                         {"keys": {"type": "string"}}, ["keys"]),
    "focus_window": _entry(focus_window, "Bring a window to the front by hwnd (from list_windows) or by a "
                                         "title substring.",
                           {"hwnd": {"type": "integer"}, "title": {"type": "string"}}),
    "open_app": _entry(open_app, "Open an application by name (notepad, calc, excel, chrome…) or by the "
                                 "path of an executable/shortcut. Opening a terminal (powershell, cmd, wt…) "
                                 "asks the user to approve; once approved, typing into it is allowed for a "
                                 "few minutes.",
                       {"app": {"type": "string"},
                        "args": {"type": "array", "items": {"type": "string"}}}, ["app"]),
    "set_clipboard": _entry(set_clipboard, "Put text on the computer's clipboard.",
                            {"text": {"type": "string"}}, ["text"]),
    "write_file": _entry(
        write_file, "Write a text file on the computer. ALWAYS asks the user to approve first.",
        {"path": {"type": "string"}, "content": {"type": "string"},
         "if_exists": {"type": "string", "enum": ["refuse", "overwrite"]}}, ["path", "content"]),
    "delete_path": _entry(
        delete_path, "Delete a file or empty directory on the computer. ALWAYS asks the user to approve first.",
        {"path": {"type": "string"}}, ["path"]),
}
