# Egress for an AstralVessel deployment: the only way out of the agent networks.
# It forwards model calls with the API key added here, so the harness and sandbox never hold it.
# proxy.py serves the checked forward proxy for webfetch and websearch.
from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import socket
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

log = logging.getLogger("egress")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
EGRESS_TOKEN = os.environ.get("EGRESS_TOKEN", "")
ALLOWED_LLM_PATHS = {"chat/completions": {"POST"}, "models": {"GET"}}


app = FastAPI(title="astralvessel egress", docs_url=None, redoc_url=None, openapi_url=None)
_client: httpx.AsyncClient | None = None


@app.on_event("startup")
async def _start() -> None:
    global _client
    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(20.0, read=None, write=60.0, pool=None),
        trust_env=False,
        follow_redirects=False,
        limits=httpx.Limits(max_connections=64),
    )
    if not LLM_API_KEY:
        log.error("LLM_API_KEY is not set; LLM calls will fail.")
    if not EGRESS_TOKEN:
        log.error("EGRESS_TOKEN is not set; every model call will be refused.")
    if not LLM_BASE_URL:
        log.error("LLM_BASE_URL is not set; every model call will be refused.")


@app.on_event("shutdown")
async def _stop() -> None:
    if _client is not None:
        await _client.aclose()


def client() -> httpx.AsyncClient:
    assert _client is not None
    return _client


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "llm": LLM_BASE_URL, "key_configured": bool(LLM_API_KEY)}


# ── LLM passthrough ───────────────────────────────────────────────────────

@app.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def llm(path: str, request: Request):
    # Other services share the egress network (search does), so only a caller holding the vessel's token may spend
    # the key. Without a configured token the passthrough stays shut.
    offered = request.headers.get("authorization", "").removeprefix("Bearer ")
    if not EGRESS_TOKEN or not hmac.compare_digest(offered.encode(), EGRESS_TOKEN.encode()):
        raise HTTPException(status_code=403, detail="the egress token is missing or wrong")
    if not LLM_BASE_URL:
        raise HTTPException(status_code=503, detail="no model endpoint is configured")
    allowed = ALLOWED_LLM_PATHS.get(path)
    if not allowed or request.method not in allowed:
        raise HTTPException(status_code=404, detail="Not a route this egress forwards.")
    body = await request.body()
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": request.headers.get("content-type", "application/json"),
        "Accept": request.headers.get("accept", "*/*"),
        # Identity so the raw upstream bytes can be relayed as they are.
        "Accept-Encoding": "identity",
    }
    upstream = client().build_request(request.method, f"{LLM_BASE_URL}/{path}", content=body, headers=headers)
    try:
        resp = await client().send(upstream, stream=True)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="model upstream unreachable") from exc
    kept = ("content-type", "cache-control", "x-request-id")
    passthrough = {k: v for k, v in resp.headers.items() if k.lower() in kept}
    return StreamingResponse(resp.aiter_bytes(), status_code=resp.status_code, headers=passthrough,
                             background=BackgroundTask(resp.aclose))


# ── public-address guard ──────────────────────────────────────────────────

class Forbidden(Exception):
    pass


def _is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    # is_global also refuses shared address space (100.64.0.0/10, which Tailscale uses).
    if not addr.is_global:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
                or addr.is_reserved or addr.is_unspecified or getattr(addr, "is_site_local", False))


def resolve_public(host: str, port: int, *, resolve=socket.getaddrinfo) -> str:
    # Every resolved address must be public; a split answer with one private address is refused.
    host = (host or "").lower().rstrip(".").strip("[]")
    if not host:
        raise Forbidden("no host")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public_ip(str(literal)):
            raise Forbidden(f"{host} is a non-public address")
        return host
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".arpa")) or "." not in host:
        raise Forbidden("internal hostnames are not reachable")
    try:
        infos = resolve(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise Forbidden("host does not resolve") from exc
    addresses = [info[4][0] for info in infos]
    if not addresses:
        raise Forbidden("host does not resolve")
    for ip in addresses:
        if not _is_public_ip(ip):
            raise Forbidden(f"{host} resolves to a non-public address")
    return addresses[0]


def validate_public_url(url: str, *, resolve=socket.getaddrinfo) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise Forbidden("only http and https URLs are reachable")
    if parts.username or parts.password:
        raise Forbidden("URLs with credentials are not reachable")
    if parts.port not in (None, 80, 443):
        raise Forbidden("only the default web ports are reachable")
    resolve_public(parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80), resolve=resolve)
    return url.strip()


@app.exception_handler(404)
async def _not_found(request: Request, exc):
    return JSONResponse(status_code=404, content={"detail": "Not a route this egress forwards."})
