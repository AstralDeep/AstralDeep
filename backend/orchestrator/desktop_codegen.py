"""Implements the offer_desktop_codegen meta-tool: generates a downloadable code
artifact plus a GitHub-release-backed, integrity-checked desktop-app download card;
invoked from orchestrator.py's chat tool loop.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from shared.feature_flags import flags
from shared.protocol import MCPResponse

logger = logging.getLogger("Orchestrator.DesktopCodegen")

META_AGENT_ID = "__desktop_codegen__"
_DESKTOP_REPO = "AstralDeep/AstralDeep"
_EXE_NAME = "AstralDeep.exe"
_SHA_NAME = "SHA256SUMS"
_BUNDLE_NAME = "cosign.bundle"
_TTL = 300

_CACHE: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}

SYSTEM_PROMPT_ADDENDUM = (
    "\n\n## Desktop code generation\n"
    "When the user asks you to generate code that should run ON THEIR MACHINE "
    "(a script to organize files, a local utility, an automation, etc.), call "
    "`offer_desktop_codegen` with the code you generated and a short note. It "
    "returns the code plus a download card for the Astral desktop app (a coding "
    "agent that can read/write files and run commands in an approved workspace, "
    "permission-gated, PHI-gated, audited). Tell the user to install it, sign in, "
    "enable the coding agent's permissions, then have it write/run the code. Do "
    "NOT call this tool for code that only needs to run in the browser/server.\n"
    "When the user just asks for the desktop app or its download link (e.g. "
    "\"where do I download the Windows app?\"), call `offer_desktop_codegen` "
    "with NO code — it returns only the verified download card for the latest "
    "release. Never paste a download URL from memory."
)


def meta_tool_definitions() -> List[Dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "offer_desktop_codegen",
                "description": (
                    "Offer generated code that runs on the user's own Windows machine, "
                    "alongside a download card for the Astral desktop app (a coding agent "
                    "that writes/runs the code locally, permission-gated + PHI-gated + "
                    "audited). Call this when the user asks for code that must execute on "
                    "their computer — NOT for browser/server-only code. ALSO call it with "
                    "no `code` when the user simply asks for the desktop app or its "
                    "Windows download link — it then returns just the verified card."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "language": {"type": "string", "description": "Code language, e.g. python"},
                        "code": {"type": "string", "description": "The generated code to show + run locally; omit to offer just the download card"},
                        "summary": {"type": "string", "description": "One-line note on what the code does"},
                    },
                    "required": [],
                },
            },
        },
    ]


def should_inject(draft_agent_id: Optional[str]) -> bool:
    return flags.is_enabled("desktop_codegen") and not draft_agent_id


def _repo() -> str:
    import os
    return os.getenv("DESKTOP_RELEASE_REPO", _DESKTOP_REPO)


def _ttl() -> int:
    import os
    try:
        return max(30, int(os.getenv("DESKTOP_RELEASE_TTL_SECONDS", str(_TTL))))
    except ValueError:
        return _TTL


def _fetch_release_info() -> Optional[Dict[str, Any]]:
    import os
    import json
    from shared import external_http
    import requests

    repo = _repo()
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        external_http.validate_egress_url(url)
    except Exception as exc:  # noqa: BLE001
        logger.info("desktop release lookup egress blocked: %s", exc)
        return None

    headers = {"Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.get(url, headers=headers, timeout=10,
                            allow_redirects=True, stream=True)
        if resp.status_code != 200:
            logger.info("github releases API returned %s", resp.status_code)
            return None
        body = b""
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            body += chunk
            if len(body) > 2 * 1024 * 1024:
                resp.close()
                return None
        data = json.loads(body.decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        logger.info("desktop release lookup failed: %s", exc)
        return None

    assets = {a.get("name"): a for a in (data.get("assets") or [])}
    exe = assets.get(_EXE_NAME) or {}
    sha = assets.get(_SHA_NAME) or {}
    bundle = assets.get(_BUNDLE_NAME) or {}
    info = {
        "version": (data.get("name") or data.get("tag_name") or "").strip(),
        "tag": (data.get("tag_name") or "").strip(),
        "exe_url": exe.get("browser_download_url") or "",
        "exe_size": exe.get("size"),
        "sha_url": sha.get("browser_download_url") or "",
        "bundle_url": bundle.get("browser_download_url") or "",
        "html_url": data.get("html_url") or "",
    }
    if not info["exe_url"]:
        logger.info("latest release has no AstralDeep.exe asset")
        return None
    return info


def _fetch_sha256(sha_url: str) -> Optional[str]:
    from shared import external_http
    import requests
    try:
        external_http.validate_egress_url(sha_url)
        resp = requests.get(sha_url, timeout=10, allow_redirects=True, stream=True)
        if resp.status_code != 200:
            return None
        text = b""
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            text += chunk
            if len(text) > 64 * 1024:
                break
        text_s = text.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None
    for line in text_s.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].endswith(_EXE_NAME):
            h = parts[0].lower()
            if len(h) == 64 and all(c in "0123456789abcdef" for c in h):
                return h
    for line in text_s.splitlines():
        h = line.strip().lower()
        if len(h) == 64 and all(c in "0123456789abcdef" for c in h):
            return h
    return None


def get_release_info(*, allow_refresh: bool = True) -> Optional[Dict[str, Any]]:
    repo = _repo()
    now = time.time()
    fetched_at, cached = _CACHE.get(repo, (0.0, None))
    if allow_refresh and (now - fetched_at) > _ttl():
        fresh = _fetch_release_info()
        if fresh is not None:
            fresh["sha256"] = (fresh.get("sha256")
                               or (fresh.get("sha_url") and _fetch_sha256(fresh["sha_url"]))
                               or "")
            _CACHE[repo] = (now, fresh)
            cached = fresh
    return cached


def build_download_card(info: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not info or not info.get("exe_url"):
        return {
            "type": "download_card",
            "variant": "unavailable",
            "title": "Astral desktop app",
            "description": ("The download link is temporarily unavailable. "
                            "You can get the app from the AstralDeep/AstralDeep "
                            "GitHub Releases page when it's back."),
            "download_url": "",
            "sha256": "",
            "sigstore_bundle_url": "",
            "integrity_doc_url": "",
            "version": "",
            "platform": "windows-x64",
            "html_url": f"https://github.com/{_repo()}/releases/latest",
        }
    return {
        "type": "download_card",
        "variant": "available",
        "title": "Astral desktop app — run this code on your PC",
        "description": ("Install the Astral desktop app, sign in, and enable the "
                        "coding agent's permissions. It can read/write files and run "
                        "commands inside a workspace you approve — every action is "
                        "permission-gated, PHI-gated (fail-closed), and audited. "
                        "Integrity is verified (SHA-256 + sigstore) before launch."),
        "download_url": info["exe_url"],
        "sha256": info.get("sha256") or "",
        "sigstore_bundle_url": info.get("bundle_url") or "",
        "integrity_doc_url": info.get("sha_url") or "",
        "version": info.get("version") or info.get("tag") or "",
        "platform": "windows-x64",
        "html_url": info.get("html_url") or "",
    }


async def handle_meta_tool(orch, tool_name: str, args: Dict[str, Any], *,
                           user_id: str, chat_id: Optional[str] = None,
                           websocket=None) -> MCPResponse:
    try:
        if tool_name == "offer_desktop_codegen":
            return await _offer(orch, args, user_id=user_id, chat_id=chat_id,
                                websocket=websocket)
        return MCPResponse(error={"message": f"Unknown meta-tool: {tool_name}",
                                  "retryable": False})
    except Exception as exc:  # noqa: BLE001
        logger.exception("desktop_codegen: meta-tool %s failed", tool_name)
        return MCPResponse(
            result={"status": "error", "detail": str(exc)[:300]},
            ui_components=[{"type": "alert", "variant": "error",
                            "message": "Offering the desktop download failed "
                                       "unexpectedly. You can retry or grab the app "
                                       "from the GitHub Releases page."}],
        )


async def _offer(orch, args: Dict[str, Any], *, user_id: str,
                 chat_id: Optional[str], websocket) -> MCPResponse:
    language = (args.get("language") or "text").strip().lower() or "text"
    code = (args.get("code") or "").strip()
    summary = (args.get("summary") or "").strip()

    info = get_release_info()
    card = build_download_card(info)
    if not code and card.get("variant") == "available":
        card["title"] = "Astral desktop app for Windows"
        card["description"] = (
            "Download and install the Astral desktop app, then sign in with "
            "your account. Integrity is verified (SHA-256 + sigstore) before "
            "the app runs.")

    components: List[Dict[str, Any]] = []
    if summary:
        components.append({"type": "text", "content": summary, "variant": "markdown"})
    if code:
        components.append({"type": "code", "code": code, "language": language})
    components.append(card)

    status = "offered" if card.get("variant") == "available" else "unavailable"
    return MCPResponse(
        result={"status": status, "version": card.get("version", ""),
                "download_url": card.get("download_url", "")},
        ui_components=components,
    )
