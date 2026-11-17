"""Runs the OAuth and MCP fixture, a scripted model server and any requested A2A agents outside pytest, printing their URLs as one JSON line on stdout.
Start it with `python -m tests.fixtures.connections.serve` from `backend`; passing `--tls-cert` and `--tls-key` together serves HTTPS, `--a2a` adds an agent, and everything binds 127.0.0.1.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading

from .a2a_agent import FixtureAgent
from .model_server import ModelServer
from .oauth_server import FixtureAuth, FixtureConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tests.fixtures.connections.serve")
    parser.add_argument("--tls-cert")
    parser.add_argument("--tls-key")
    parser.add_argument("--config", help="JSON object of FixtureConfig switches")
    parser.add_argument("--a2a", action="append", default=[], help='JSON agent spec such as {"scheme": "http_bearer", "signed": true}')
    args = parser.parse_args(argv)
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error("--tls-cert and --tls-key go together")
    tls = (args.tls_cert, args.tls_key) if args.tls_cert else None
    try:
        config = FixtureConfig.from_json(json.loads(args.config)) if args.config else FixtureConfig()
    except (ValueError, TypeError) as error:
        parser.error(f"--config: {error}")
    agents: list[FixtureAgent] = []
    try:
        for raw in args.a2a:
            agents.append(FixtureAgent.from_json(json.loads(raw), tls))
    except (ValueError, TypeError) as error:
        for started in agents:
            started.stop()
        parser.error(f"--a2a: {error}")
    auth = FixtureAuth(config, tls=tls)
    urls = auth.start()
    model = ModelServer(tls=tls)
    done = threading.Event()
    for name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(name, lambda *_: done.set())
    report = {
        "issuer": urls.issuer,
        "authorization_server": urls.as_origin,
        "resource_server": urls.rs_origin,
        "mcp": urls.rs_url,
        "model": model.url,
    }
    if agents:
        report["a2a"] = [
            {
                "scheme": agent.scheme,
                "signed": agent.signed,
                "card": agent.card_url,
                "rpc": agent.rpc_url,
                "credential_headers": agent.credential_headers(),
                "rpc_target": agent.rpc_target,
            }
            for agent in agents
        ]
    print(json.dumps(report), flush=True)
    done.wait()
    for agent in agents:
        agent.stop()
    model.stop()
    auth.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
