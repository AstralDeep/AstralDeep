"""Builds bounded commands and validates the host Chrome DevTools endpoint used by independent review."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Any

CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
_BROWSER_MARKERS = re.compile(r"(?:^|[/@])(?:vite|next|react|react-dom|vue|svelte|astro|nuxt|angular|solid-js|preact|qwik)(?:$|[/@])", re.I)
_SCRIPT_MARKERS = re.compile(r"(?:^|[\s;&|])(?:vite|next|astro|nuxt|react-scripts|ng|parcel|webpack)(?:\s|$)", re.I)
_PORT = re.compile(r"^[1-9][0-9]{0,4}$")
_BROWSER_PATH = re.compile(r"^/devtools/browser/[A-Fa-f0-9-]{16,64}$")


def is_web_project(entries: list[Any], manifest_text: str) -> bool:
    names = {
        row.get("name") for row in entries
        if isinstance(row, dict) and isinstance(row.get("name"), str) and row.get("is_dir") is not True
    }
    if "package.json" not in names:
        return False
    try:
        manifest = json.loads(manifest_text)
    except (json.JSONDecodeError, TypeError):
        return False
    if type(manifest) is not dict:
        return False
    scripts = manifest.get("scripts") if type(manifest.get("scripts")) is dict else {}
    dependencies = {}
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        value = manifest.get(section)
        if type(value) is dict:
            dependencies.update(value)
    return (any(_SCRIPT_MARKERS.search(value) for value in scripts.values() if isinstance(value, str))
            or any(_BROWSER_MARKERS.search(name) for name in dependencies)
            or "index.html" in names)


def launch_arguments(root: str, profile_name: str) -> dict[str, Any]:
    profile = str(PurePosixPath(root) / profile_name)
    return {
        "argv": [
            CHROME_PATH,
            "--headless=new",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--user-data-dir=" + profile,
            "about:blank",
        ],
        "cwd": root,
        "timeout_s": 660,
        "background": True,
    }


def active_port_path(root: str, profile_name: str) -> str:
    return str(PurePosixPath(root) / profile_name / "DevToolsActivePort")


def parse_active_port(text: str) -> tuple[int, str] | None:
    if not isinstance(text, str) or len(text) > 256:
        return None
    lines = text.splitlines()
    if len(lines) != 2 or not _PORT.fullmatch(lines[0]) or not _BROWSER_PATH.fullmatch(lines[1]):
        return None
    port = int(lines[0])
    return (port, lines[1]) if 1 <= port <= 65535 else None


def endpoint(port: int) -> str:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("invalid Chrome DevTools port")
    return f"http://localhost:{port}"


def endpoint_probe_script() -> str:
    return (
        "fetch(process.argv[1],{signal:AbortSignal.timeout(8000)}).then(async r=>{"
        "const v=await r.json();if(!r.ok||typeof v.Browser!=='string'||!v.Browser.startsWith('Chrome/')||"
        "typeof v.webSocketDebuggerUrl!=='string')process.exitCode=2;else{const ws=new URL(v.webSocketDebuggerUrl);"
        "const u=new URL(process.argv[1]);if(ws.protocol!=='ws:'||ws.port!==u.port||ws.pathname!==process.argv[2])"
        "process.exitCode=2;else console.log('Chrome DevTools ready')}"
        "}).catch(()=>{process.exitCode=1})"
    )


def reviewer_browser_hint(url: str) -> str:
    match = re.fullmatch(r"http://localhost:([1-9][0-9]{0,4})", url) if isinstance(url, str) else None
    if match is None or int(match.group(1)) > 65535:
        raise ValueError("invalid local Chrome endpoint")
    return (
        "A host-owned isolated headless Chrome browser is already running for this review at " + url + ". "
        "Use Playwright Chromium `connectOverCDP(" + json.dumps(url) + ")` to attach to this browser. "
        "Do not call chromium.launch, install browser binaries, or create another browser/profile. "
        "Run the requested interaction checks in the attached browser and cite what you observed."
    )
