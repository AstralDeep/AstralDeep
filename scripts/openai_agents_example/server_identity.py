"""Observes a directly published local candidate container without reading credentials.
The qualification runner binds this serving image separately from its client checkout.
"""

from __future__ import annotations

import json
import re
import subprocess
from urllib.parse import urlsplit

_FORMAT = ('{"container_id":{{json .Id}},"image_digest":{{json .Image}},'
           '"running":{{json .State.Running}},"ports":{{json .NetworkSettings.Ports}}}')
_IPV4 = frozenset({"127.0.0.1", "0.0.0.0"})
_IPV6 = frozenset({"::1", "::"})
_BINDINGS = {"127.0.0.1": (_IPV4,), "::1": (_IPV6,), "localhost": (_IPV4, _IPV6)}


def observe_server(base_url: str, container: str | None) -> dict | None:
    try:
        url = urlsplit(base_url)
        port = url.port or (443 if url.scheme == "https" else 80)
        if (not container or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container)
                or url.scheme not in ("http", "https") or url.hostname not in ("localhost", "127.0.0.1", "::1")
                or url.username or url.password or url.query or url.fragment or url.path not in ("", "/")):
            return None
        selected = subprocess.run(
            ["docker", "context", "show"], capture_output=True, text=True, timeout=10, check=False,
        )
        name = selected.stdout.strip()
        if selected.returncode != 0 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
            return None
        context = subprocess.run(
            ["docker", "context", "inspect", name, "--format", "{{json .Endpoints.docker.Host}}"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if context.returncode != 0 or len(context.stdout) > 4096:
            return None
        endpoint = json.loads(context.stdout)
        if not isinstance(endpoint, str) or not endpoint.startswith(("unix://", "npipe://")):
            return None
        result = subprocess.run(
            ["docker", "--context", name, "inspect", "--type", "container", "--format", _FORMAT, container],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode != 0 or len(result.stdout) > 65536:
            return None
        identity = json.loads(result.stdout)
        if (type(identity) is not dict or identity.get("running") is not True
                or not re.fullmatch(r"[0-9a-f]{64}", str(identity.get("container_id")))
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(identity.get("image_digest")))
                or type(identity.get("ports")) is not dict):
            return None
        bindings = identity["ports"].get("8001/tcp")
        if (type(bindings) is not list or not all(any(
                type(item) is dict and item.get("HostPort") == str(port)
                and item.get("HostIp") in family for item in bindings) for family in _BINDINGS[url.hostname])):
            return None
        return {"container_id": identity["container_id"], "image_digest": identity["image_digest"],
                "docker_context": name,
                "published_port": port, "observation": "local_docker_direct_publish"}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None
