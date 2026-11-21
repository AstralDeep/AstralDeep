"""Renders the canonical macOS Seatbelt profile for a policy loaded for darwin, line for line as the execution policy contract fixes it.
launch.py writes the text to a private file for sandbox-exec, and the committed profile vectors pin every byte.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .constants import (
    DARWIN_BASE,
    DARWIN_EXEC_BASE,
    DARWIN_EXEC_ONLY,
    DARWIN_KEYCHAINS,
    DARWIN_METADATA_ONLY,
    DENIED_LOG_SERVICES,
    DENIED_SERVICES,
    DNSSD_SERVICE,
    SYSLOG_SOCKET,
)
from .errors import ContextError
from .fold import assigned
from .paths import path_problem
from .policy import Policy

_CONTEXT_KEYS = frozenset({"proxy_port", "home"})


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _checked_home(home: Any) -> str:
    if not isinstance(home, str) or path_problem(home):
        raise ContextError("home must be an absolute normalized path")
    if not all(map(assigned, home)):
        raise ContextError("home holds an unassigned code point")
    return home


def _profile(document: dict[str, Any], home: str, proxy_port: Any) -> str:
    if document["hosts"] and not (_is_int(proxy_port) and 1 <= proxy_port <= 65535):
        raise ContextError("a policy with hosts needs a proxy port from 1 to 65535")
    workspace = quote(document["workspace"])
    reads = document["reads"]
    lines = ["(version 1)", "(deny default)", '(import "system.sb")', ""]
    lines += [
        f"(allow process-exec* (subpath {quote(root)}))"
        for root in (*DARWIN_EXEC_BASE, *reads, *DARWIN_EXEC_ONLY)
    ]
    if document["limits"]["processes"] > 1:
        lines.append("(allow process-fork)")
    lines += [
        "(allow process-info* (target self))",
        "(allow signal (target self))",
        "(allow signal (target children))",
        "(allow sysctl-read)",
        "(allow file-ioctl)",
        "",
    ]
    lines += [f"(allow file-read* (subpath {quote(root)}))" for root in DARWIN_BASE]
    metadata = " ".join(f"(subpath {quote(root)})" for root in DARWIN_METADATA_ONLY)
    lines.append(f"(allow file-read-metadata {metadata})")
    lines += [f"(allow file-read* (subpath {quote(root)}))" for root in reads]
    lines += [f"(allow file-read* file-write* (subpath {workspace}))", ""]
    services = " ".join(f'(global-name "{name}")' for name in DENIED_SERVICES)
    loggers = " ".join(f'(global-name "{name}")' for name in DENIED_LOG_SERVICES)
    lines += [
        f"(deny process-exec* (subpath {workspace}))",
        f"(deny file-map-executable (subpath {workspace}))",
        f'(deny mach-lookup (global-name "{DNSSD_SERVICE}"))',
        f'(deny mach-lookup {services} (xpc-service-name-prefix ""))',
        f"(deny mach-lookup {loggers})",
        f"(deny network-outbound (literal {quote(SYSLOG_SOCKET)}))",
    ]
    lines += [
        f"(deny file-read* (subpath {quote(root)}))"
        for root in (home + "/Library/Keychains", *DARWIN_KEYCHAINS)
    ]
    if document["hosts"]:
        lines += ["", f'(allow network-outbound (remote ip "localhost:{proxy_port}"))']
    return "\n".join(lines) + "\n"


def _render_document(document: Any, home: Any, proxy_port: Any) -> str:
    checked = Policy.parse(document, "darwin").document
    return _profile(checked, _checked_home(home), proxy_port)


def render(policy: Policy, context: Mapping[str, Any]) -> str:
    if not isinstance(policy, Policy) or policy.loaded_for is None:
        raise ContextError("render needs a policy loaded with Policy.load")
    platform, _data_dir, home = policy.loaded_for
    if platform != "darwin":
        raise ContextError("the Seatbelt profile needs a policy loaded for darwin")
    if not isinstance(context, Mapping) or "proxy_port" not in context:
        raise ContextError("the render context must carry proxy_port")
    if set(context) - _CONTEXT_KEYS:
        raise ContextError(
            f"unknown render context keys: {sorted(set(context) - _CONTEXT_KEYS)}"
        )
    if "home" in context and context["home"] != home:
        raise ContextError(
            "the render context names another home than the loaded policy"
        )
    document = Policy.parse(policy.canonical_bytes(), "darwin").document
    return _profile(document, home, context["proxy_port"])
