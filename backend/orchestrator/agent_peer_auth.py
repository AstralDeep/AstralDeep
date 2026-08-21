"""Outbound peer credential for agents the orchestrator dials OUT to.

``AGENT_API_KEY`` has always authenticated ONE direction: a connecting agent
proves possession to the orchestrator (``orchestrator.auth.validate_agent_api_key``).
Nothing proved anything to the *agent*, so a client-hosted agent — notably the
Windows client's tools agent, which reads/writes files and runs commands on a
user's PC — had no way to tell the orchestrator apart from any other host that
could reach its port. This module supplies the missing half: the orchestrator
presents the same shared secret on its outbound connections, and the agent
verifies it.

**The destination gate is the load-bearing part.** ``AGENT_API_KEY`` is the
fleet-wide registration secret — any holder can register or overwrite any agent
id — and ``register_external_agent`` accepts a raw user-supplied URL with no
egress validation. Attaching the key to every outbound discovery would let any
authenticated user make the orchestrator hand the fleet secret to a host of
their choosing, which is a strictly worse hole than the one being closed. The
key is therefore sent ONLY to destinations the operator has declared:

* loopback in any spelling (``localhost``, ``127.0.0.0/8``, ``::1``) — the
  first-party agents and draft subprocesses the orchestrator itself spawns;
* the Docker host aliases (``host.docker.internal``, ``gateway.docker.internal``)
  — how a containerized orchestrator reaches a desktop client-hosted agent;
* every host named in ``AGENT_KEY_TRUSTED_HOSTS``.

Anything else — including a URL a user typed into ``register_external_agent`` —
gets no credential. Such an agent simply registers unauthenticated exactly as it
does today; nothing regresses, and the secret does not travel.

``A2A_EXTERNAL_AGENTS`` is deliberately NOT trusted here. It is a *discovery*
list — "these agents exist, go talk to them" — and an operator who wrote a
third-party partner into it never consented to that partner receiving the
fleet-wide registration secret. Silently promoting a discovery list into a
credential-distribution list on upgrade is exactly the kind of surprise this
module exists to prevent. Trusting such a peer is a separate, explicit decision:
name it in ``AGENT_KEY_TRUSTED_HOSTS`` too.
"""
from __future__ import annotations

import ipaddress
import logging
import os
from urllib.parse import urlparse

logger = logging.getLogger("orchestrator.agent_peer_auth")

#: Request header carrying the shared agent key on outbound orchestrator→agent
#: connections. Deliberately NOT ``Authorization``: that header is already used
#: on the agent-facing A2A path for the per-call RFC 8693 delegation token
#: (``_execute_via_a2a``), which is a different credential with different
#: lifetime and scope. ``X-Astral-*`` matches the repo's existing inter-component
#: header convention (``X-Astral-Voice-Worker``, ``X-Astral-Device-Id``, …).
AGENT_KEY_HEADER = "X-Astral-Agent-Key"

#: Destinations that are trustworthy by construction, not by configuration.
_STATIC_TRUSTED_HOSTS = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "::1",
        "host.docker.internal",
        "gateway.docker.internal",
    }
)

#: The authentication scheme an Astral agent names in its 401 challenge. Both
#: sides must spell this identically (pinned by a cross-file drift test in
#: components/AstralProjection/windows-client/tests/test_win_agent_inbound_auth.py).
AGENT_AUTH_SCHEME = "AstralAgentKey"


def peer_demands_agent_key(www_authenticate) -> bool:
    """True when a 401's ``WWW-Authenticate`` names OUR challenge scheme.

    This is the "ping the port first" half of the destination gate. Host
    trust alone is coarse: it covers a whole machine, so ANY port on loopback or
    the Docker host would otherwise receive the key — including some unrelated
    dev server, debug port or database that happens to be listening there.

    Requiring the peer to first answer an UNAUTHENTICATED request with this
    exact challenge means the credential only ever reaches something that has
    demonstrated it runs our gate and is waiting for this specific scheme. A
    service that is not an Astral agent never sees the key, even on a fully
    trusted host.

    This does not stop someone who deliberately stands up an impostor that
    echoes the challenge — for that they must already be able to run code on a
    trusted host, at which point they are inside the trust boundary. It closes
    the accidental-disclosure case, which is the realistic one.
    """
    if not www_authenticate:
        return False
    # Scheme tokens are case-insensitive (RFC 7235) and may be followed by
    # params ("AstralAgentKey realm=..."), or appear in a comma-separated list.
    for challenge in str(www_authenticate).split(","):
        if challenge.strip().split(" ")[0].lower() == AGENT_AUTH_SCHEME.lower():
            return True
    return False


def _configured_key() -> str:
    """The shared agent key, or ``""`` when unset. Read per call (not cached at
    import) so tests can monkeypatch the environment.

    A non-ASCII key is treated as unset: HTTP header values must be latin-1
    encodable, so sending one raises inside httpx/aiohttp — and every one of
    those call sites is wrapped in a broad ``except`` that logs at DEBUG, so the
    failure would present as "the agent silently never registers" rather than as
    a bad key. Refusing it here makes the degradation the honest one (no
    credential sent) instead of a swallowed transport error.
    """
    key = os.getenv("AGENT_API_KEY", "").strip()
    if key and not key.isascii():
        logger.warning(
            "AGENT_API_KEY is not ASCII; it cannot be sent as an HTTP header, "
            "so no credential will be presented to agents"
        )
        return ""
    return key


#: What replaces the secret in a log line.
_REDACTION = "<redacted:agent-key>"

#: Below this length a key is not substring-replaced: swapping out a 2-character
#: value would mangle unrelated text. Such a key is refused by the agent's own
#: gate anyway, so there is nothing worth protecting.
_MIN_REDACTABLE = 8

_redaction_installed = False


def _redact(value, key: str):
    if isinstance(value, str) and key in value:
        return value.replace(key, _REDACTION)
    return value


def install_key_redaction() -> None:
    """Scrub the shared agent key out of every log record, from any library.

    Our own code never logs the key, but third-party libraries do: the
    ``websockets`` client logs full handshake headers at DEBUG, so an
    orchestrator running at DEBUG would write the fleet secret to disk in
    cleartext on every agent dial. aiohttp/httpx can do the same under their own
    debug settings.

    This hooks the **record factory**, not a logger filter. A ``logging.Filter``
    attached to a logger only sees records logged *directly through that logger*
    — it is NOT consulted for records propagated up from child loggers, so a
    filter on ``websockets`` would miss everything ``websockets.client`` emits,
    which is exactly where the handshake dump lives. The factory runs for every
    record ever created, whatever the logger name or handler configuration.

    Rewrites rather than drops, so the diagnostic value of the line survives and
    only the secret is replaced. Reads the key per record, so a rotated key is
    redacted too, and never raises (a raising factory would break all logging).
    """
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
        except Exception:  # noqa: BLE001 — never let redaction break logging
            pass
        return record

    logging.setLogRecordFactory(_factory)


install_key_redaction()


def _declared_hosts() -> set:
    """Hosts the operator has explicitly declared may hold the shared key.

    Accepts both full URLs and bare host[:port] entries, so an operator can
    write either form. NOTE this reads only ``AGENT_KEY_TRUSTED_HOSTS`` — see
    the module docstring on why ``A2A_EXTERNAL_AGENTS`` is not folded in.
    """
    out = set()
    raw = os.getenv("AGENT_KEY_TRUSTED_HOSTS", "")
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            host = urlparse(item if "://" in item else "http://" + item).hostname
        except ValueError:  # malformed entry — ignore rather than trust
            continue
        if host:
            out.add(host.lower())
    return out


def trusted_agent_destination(base_url: str) -> bool:
    """True when ``base_url``'s host may receive the shared agent key."""
    try:
        host = (urlparse(base_url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    if host in _STATIC_TRUSTED_HOSTS or host in _declared_hosts():
        return True
    try:
        # 127.0.0.0/8 and ::1 in any spelling (127.0.0.2, ::ffff:127.0.0.1, …)
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def agent_auth_headers(base_url: str) -> dict:
    """The outbound credential headers for ``base_url``.

    ``{}`` when no key is configured or the destination is not operator-declared
    — callers spread this unconditionally (``headers=agent_auth_headers(url)``)
    and an untrusted destination simply gets no credential.
    """
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
    """``http(s)://host:port[/prefix]`` → ``ws(s)://host:port[/prefix]/agent``.

    Preserves TLS: the previous inline expression hardcoded ``ws://`` and
    stripped ``https://``, silently downgrading an https agent to plaintext —
    which would now also put the shared key on the wire in the clear. It also
    preserves any path prefix, so an agent mounted behind a reverse proxy at
    ``https://host/agents/win`` keeps its WebSocket transport.
    """
    # A scheme-less base URL must be normalized BEFORE parsing: urlparse reads
    # the "host:" of "host:8771" as a scheme and leaves netloc empty, so the
    # port would become the host. Same trick _declared_hosts uses.
    raw = base_url if "://" in base_url else "http://" + base_url
    parsed = urlparse(raw)
    # 073: an already-websocket scheme passes through. Writing a remote agent in
    # as `wss://host` is a natural mistake now that A2A_EXTERNAL_AGENTS names
    # WebSocket agents on other hosts, and mapping it to `ws` would silently
    # downgrade exactly the TLS this function exists to preserve.
    if parsed.scheme in ("ws", "wss"):
        scheme = parsed.scheme
    else:
        scheme = "wss" if parsed.scheme == "https" else "ws"
    return "{}://{}{}/agent".format(
        scheme, parsed.netloc, parsed.path.rstrip("/")
    )
