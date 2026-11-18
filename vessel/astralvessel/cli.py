# Command line entry points: up, check, down, pair and connections for the operator, ask and chat with a caller
# ticket, and caller key for the holder key. deploy writes Compose from a deployment file; executor and sandbox run
# in their containers.

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from .caller import REMOVED_OPTIONS, TICKET_FILE_VARIABLE

TICKET_HELP = f"a file holding the caller ticket (default: the file named by ${TICKET_FILE_VARIABLE})"


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


class Removed(argparse.Action):
    # An option from an earlier release: say why it is gone instead of a bare "unrecognized arguments".
    def __call__(self, parser, namespace, values, option_string=None):
        parser.error(f"{option_string} is gone: {REMOVED_OPTIONS[option_string]}")


def removed(command: argparse.ArgumentParser, *names: str) -> None:
    for name in names:
        command.add_argument(name, action=Removed, nargs="?", help=argparse.SUPPRESS)


def port_number(text: str) -> int:
    if not text.isdigit() or int(text) > 65535:
        raise argparse.ArgumentTypeError("a port is a number from 0 to 65535")
    return int(text)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="astralvessel")
    sub = root.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up", help="start the node, writing a deployment first if there is none; then check it")
    up.add_argument("deployment", nargs="?", help="deployment file (default: deployment.toml here)")
    up.add_argument("--image", help="the AstralDeep node image to run")
    up.add_argument("--port", type=int, help="the local port to publish the node on (default 8001)")
    up.add_argument("--upstream-url", help="a model gateway: the provider's OpenAI-compatible base URL")
    up.add_argument("--api-key", metavar="env:NAME", help="read the gateway's provider key from this variable")
    up.add_argument("--example", action="store_true", help="add the example field guide agent")
    up.add_argument("--agent", action="append", metavar="FOLDER", help="an agent folder (repeatable)")
    up.add_argument("--mount", action="append", metavar="AGENT:/TARGET=FOLDER", help="a folder an agent needs")
    up.add_argument("--timeout", type=float, default=300, help="seconds to wait for the node and sandboxes (300)")
    up.add_argument("--bundle", help="the node pack to run: its node-pack.json, or the folder that holds it")
    up.add_argument("--bundle-sha256", help="the SHA-256 that node-pack.json must have")
    up.add_argument("--foreground", action="store_true",
                    help="supervise the processes in this terminal, with no systemd or launchd")
    check = sub.add_parser("check", help="run the isolation probes and the node health and descriptor checks")
    check.add_argument("deployment", nargs="?")
    check.add_argument("--bundle", help="the node pack, when the deployment file names none")
    check.add_argument("--bundle-sha256")
    check.add_argument("--isolation-only", action="store_true", help="skip the node checks, for hosts with no node")
    suite = sub.add_parser("conformance", help="run the acceptance checks against the running node")
    suite.add_argument("--deployment")
    suite.add_argument("--ticket-file", help=TICKET_HELP)
    removed(suite, "--token-file", "--key-file")
    pair = sub.add_parser("pair", help="print the node's pairing code and payload for the AstralDeep app")
    for name, help_text in (("ask", "ask the node one question"), ("chat", "chat with the node")):
        talk = sub.add_parser(name, help=help_text)
        if name == "ask":
            talk.add_argument("question")
        talk.add_argument("--deployment")
        talk.add_argument("--ticket-file", help=TICKET_HELP)
        removed(talk, "--token-file", "--key-file")
    pair.add_argument("--executor", action="store_true",
                      help="print the executor's offer instead, so it can check the owner's tickets")
    pair.add_argument("--deployment")
    removed(pair, "--token-file")
    caller = sub.add_parser("caller", help="manage the holder key that signs this machine's caller requests")
    caller_commands = caller.add_subparsers(dest="caller_command", required=True)
    key = caller_commands.add_parser("key", help="create the private caller-key file and print its public holder key")
    key.add_argument("--deployment", help="deployment file; the key goes in its folder (default: deployment.toml here)")
    links = sub.add_parser("connections", help="list the node's outside connections and sign the node in to one")
    link_commands = links.add_subparsers(dest="connections_command", required=True)
    listing = link_commands.add_parser("list", help="show each connection and whether the node is signed in to it")
    signing = link_commands.add_parser("sign-in", help="sign the node in to a connection from this machine")
    signing.add_argument("connection", help="the connection's name or id (or the start of its id)")
    how = signing.add_mutually_exclusive_group()
    how.add_argument("--paste", action="store_true", help="print the address, then read the address you end on")
    how.add_argument("--device-code", action="store_true", help="print a code to enter on any device")
    signing.add_argument("--port", type=port_number, default=0,
                         help="the local port for the browser redirect (default: any)")
    for command in (listing, signing):
        command.add_argument("--deployment", help="deployment file (default: deployment.toml here)")
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
    if args.command == "caller":
        operate.caller_key(args)
        return
    if args.command == "connections":
        operate.connections_command(args)
        return
    sys.stdout.reconfigure(line_buffering=True)  # keep our lines in order with docker's when logged
    getattr(operate, "conformance_suite" if args.command == "conformance" else args.command)(args)


if __name__ == "__main__":
    main()
