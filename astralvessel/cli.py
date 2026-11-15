# Command line entry points: up, check, down, pair, ask and chat for the owner; deploy writes Compose from a
# deployment file; executor and sandbox run inside their containers, configured through the environment.

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def deploy(args: argparse.Namespace) -> None:
    from .operate import write_compose
    from .stack import Stack
    stack = Stack.at(args.deployment)
    if args.env_file:
        stack.env_file = Path(args.env_file)
    deployment, out = write_compose(stack, Path(args.out).resolve() if args.out else None)
    stack.compose_file = out
    command = stack.compose_command()
    if args.up:
        subprocess.run([*command, "up", "-d", "--build", "--remove-orphans"], check=True)
    else:
        print("start it with: " + " ".join([*command, "up", "-d", "--build"]))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="astralvessel")
    sub = root.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up", help="start the node, writing a deployment first if there is none; then check it")
    up.add_argument("deployment", nargs="?", help="deployment file (default: deployment.toml here)")
    up.add_argument("--issuer", help="the owner's OIDC issuer URL")
    up.add_argument("--client-id", help="the client id the owner's apps sign in with")
    up.add_argument("--subject", help="bind the node to this issuer subject now, instead of at first pairing")
    up.add_argument("--image", help="the AstralDeep node image to run")
    up.add_argument("--port", type=int, help="the local port to publish the node on (default 8001)")
    up.add_argument("--upstream-url", help="a model gateway: the provider's OpenAI-compatible base URL")
    up.add_argument("--api-key", metavar="env:NAME", help="read the gateway's provider key from this variable")
    up.add_argument("--example", action="store_true", help="add the example field guide agent")
    up.add_argument("--agent", action="append", metavar="FOLDER", help="an agent folder (repeatable)")
    up.add_argument("--mount", action="append", metavar="AGENT:/TARGET=FOLDER", help="a folder an agent needs")
    up.add_argument("--timeout", type=float, default=300, help="seconds to wait for the node and sandboxes (300)")
    check = sub.add_parser("check", help="run the isolation probes and the node health and descriptor checks")
    check.add_argument("deployment", nargs="?")
    check.add_argument("--isolation-only", action="store_true", help="skip the node checks, for hosts with no node")
    suite = sub.add_parser("conformance", help="run the acceptance checks against the running node")
    suite.add_argument("--deployment")
    suite.add_argument("--token-file", help="a file holding the owner's access token (default: $ASTRAL_OWNER_TOKEN)")
    pair = sub.add_parser("pair", help="print the node's pairing code and payload for the AstralDeep app")
    pair.add_argument("--label", help="a name for the offer (default: astralvessel)")
    for name, help_text in (("ask", "ask the node one question"), ("chat", "chat with the node")):
        talk = sub.add_parser(name, help=help_text)
        if name == "ask":
            talk.add_argument("question")
        talk.add_argument("--deployment")
        talk.add_argument("--token-file", help="a file holding the owner's access token (default: $ASTRAL_OWNER_TOKEN)")
    pair.add_argument("--executor", action="store_true",
                      help="print the executor's offer instead, so it can check the owner's tickets")
    pair.add_argument("--deployment")
    pair.add_argument("--token-file", help="a file holding the owner's access token (default: $ASTRAL_OWNER_TOKEN)")
    down = sub.add_parser("down", help="stop the deployment")
    down.add_argument("deployment", nargs="?")
    down.add_argument("--volumes", action="store_true", help="also delete the node's data volume")
    dep = sub.add_parser("deploy", help="write a Compose file from deployment.toml")
    dep.add_argument("deployment", nargs="?", default="deployment.toml")
    dep.add_argument("--out", help="Compose file to write (default: compose.generated.yml beside the deployment)")
    dep.add_argument("--env-file", help="environment file to read (default: .env beside the deployment)")
    dep.add_argument("--up", action="store_true", help="also run docker compose up")
    executor = sub.add_parser("executor", help="run the executor that verifies tickets in front of the sandboxes")
    executor.add_argument("--host", default=os.environ.get("VESSEL_HOST", "127.0.0.1"))
    executor.add_argument("--port", type=int, default=int(os.environ.get("VESSEL_PORT", "8700")))
    sandbox = sub.add_parser("sandbox", help="run one agent's sandbox tool server")
    sandbox.add_argument("package", nargs="?", default=os.environ.get("VESSEL_PACKAGE"))
    sandbox.add_argument("--host", default=os.environ.get("VESSEL_HOST", "127.0.0.1"))
    sandbox.add_argument("--port", type=int, default=int(os.environ.get("VESSEL_PORT", "8710")))
    return root


def main() -> None:
    args = parser().parse_args()
    if args.command == "deploy":
        deploy(args)
        return
    if args.command == "executor":
        import uvicorn

        from .executor import build
        uvicorn.run(build(), host=args.host, port=args.port, log_level="info", proxy_headers=False)
        return
    if args.command == "sandbox":
        if not args.package:
            raise SystemExit("give an agent package directory or set VESSEL_PACKAGE")
        import uvicorn

        from .sandbox import sandbox_app
        uvicorn.run(sandbox_app(args.package), host=args.host, port=args.port, log_level="info",
                    proxy_headers=False)
        return
    from . import operate
    sys.stdout.reconfigure(line_buffering=True)  # keep our lines in order with docker's when logged
    getattr(operate, "conformance_suite" if args.command == "conformance" else args.command)(args)


if __name__ == "__main__":
    main()
