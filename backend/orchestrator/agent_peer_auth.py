"""Outbound credential the orchestrator presents when dialing agents it connects OUT to,
the inverse of orchestrator.auth.validate_agent_api_key. Restricts AGENT_API_KEY to
operator-declared or loopback hosts and scrubs it from logs.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from urllib.parse import urlparse

logger = logging.getLogger("orchestrator.agent_peer_auth")

AGENT_KEY_HEADER = "X-Astral-Agent-Key"

_STATIC_TRUSTED_HOSTS = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "::1",
        "host.docker.internal",
        "gateway.docker.internal",
    }
)

AGENT_AUTH_SCHEME = "AstralAgentKey"


def peer_demands_agent_key(www_authenticate) -> bool:
    if not www_authenticate:
        return False
    for challenge in str(www_authenticate).split(","):
        if challenge.strip().split(" ")[0].lower() == AGENT_AUTH_SCHEME.lower():
            return True
    return False


def _configured_key() -> str:
    key = os.getenv("AGENT_API_KEY", "").strip()
    if key and not key.isascii():
        logger.warning(
            "AGENT_API_KEY is not ASCII; it cannot be sent as an HTTP header, "
            "so no credential will be presented to agents"
        )
        return ""
    return key


_REDACTION = "<redacted:agent-key>"

_MIN_REDACTABLE = 8

_redaction_installed = False


def _redact(value, key: str):
    if isinstance(value, str) and key in value:
        return value.replace(key, _REDACTION)
    return value


def install_key_redaction() -> None:
    global _redaction_installed
    if _redaction_installed:
        return
    _redaction_installed = True
    previous = logging.getLogRecordFactory()

    def _factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        try:
            key = os.getenv("AGENT_API_KEY", "").strip()
            if not key or len(key) < _MIN_REDACTABLE:
                return record
            record.msg = _redact(record.msg, key)
            if isinstance(record.args, dict):
                record.args = {k: _redact(v, key) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(_redact(v, key) for v in record.args)
        except Exception:  # noqa: BLE001
            pass
        return record

    logging.setLogRecordFactory(_factory)


install_key_redaction()


def _declared_hosts() -> set:
    out = set()
    raw = os.getenv("AGENT_KEY_TRUSTED_HOSTS", "")
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            host = urlparse(item if "://" in item else "http://" + item).hostname
        except ValueError:
            continue
        if host:
            out.add(host.lower())
    return out


def trusted_agent_destination(base_url: str) -> bool:
    try:
        host = (urlparse(base_url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    if host in _STATIC_TRUSTED_HOSTS or host in _declared_hosts():
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def agent_auth_headers(base_url: str) -> dict:
    key = _configured_key()
    if not key:
        return {}
    if not trusted_agent_destination(base_url):
        logger.debug(
            "not presenting the agent key to an undeclared destination "
            "(add its host to AGENT_KEY_TRUSTED_HOSTS if it is yours)"
        )
        return {}
    return {AGENT_KEY_HEADER: key}


def agent_ws_url(base_url: str) -> str:
    raw = base_url if "://" in base_url else "http://" + base_url
    parsed = urlparse(raw)
    # Preserves wss here — avoids a silent TLS downgrade
    if parsed.scheme in ("ws", "wss"):
        scheme = parsed.scheme
    else:
        scheme = "wss" if parsed.scheme == "https" else "ws"
    return "{}://{}{}/agent".format(
        scheme, parsed.netloc, parsed.path.rstrip("/")
    )
