# The connection commands against a fake node: each sign-in mode posts what the node's operator API expects, the
# operator secret stays inside the container script, and check reports attention and legacy connectors.

from __future__ import annotations

import argparse
import http.client
import json
import os
import subprocess
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from astralvessel import cli, connections, operate

SECRET = "operator-secret-value-0123456789"
ID_A = "a" * 32
ID_B = "b" * 32
ROWS = [{"id": ID_A, "name": "Notes", "status": "ok", "state": "not_signed_in", "reason": None},
        {"id": ID_B, "name": "Search", "status": "ok", "state": "needs_attention", "reason": "expired"}]


class Node(BaseHTTPRequestHandler):
    seen: list = []
    answers: dict = {}

    def handle_any(self):
        size = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(size)) if size else None
        Node.seen.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        if self.headers.get("Astral-Node-Operator") != SECRET:
            queue = [(401, {"detail": "node.operator_secret_required"})]
        else:
            queue = Node.answers.get((self.command, self.path), [(404, {"detail": "node.flow_unknown"})])
        status, answer = queue.pop(0) if len(queue) > 1 else queue[0]
        retry = answer.pop("retry-after", None) if isinstance(answer, dict) else None
        raw = json.dumps(answer).encode() if not isinstance(answer, bytes) else answer
        self.send_response(status)
        if retry:
            self.send_header("Retry-After", retry)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = do_DELETE = handle_any

    def log_message(self, *args):
        return None


class FakeStack:
    def __init__(self, directory, port):
        self.env = {**os.environ, "ASTRAL_NODE_DATA_DIR": str(directory), "ORCHESTRATOR_PORT": str(port)}
        self.scripts = []

    def python(self, service, source, interpreter="python", timeout=300):
        self.scripts.append((service, source))
        return subprocess.run([sys.executable, "-"], input=source, capture_output=True, text=True, env=self.env)


@pytest.fixture
def node(tmp_path):
    Node.seen, Node.answers = [], {("GET", "/api/node/connections"): [(200, {"connections": ROWS})]}
    (tmp_path / "operator-secret").write_text(SECRET + "\n")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Node)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield FakeStack(tmp_path, server.server_address[1])
    server.shutdown()


def posts():
    return [(path, body) for method, path, _, body in Node.seen if method == "POST"]


def deletes():
    return [path for method, path, _, _ in Node.seen if method == "DELETE"]


def start_path(connection_id=ID_A):
    return ("POST", f"/api/node/connections/{connection_id}/sign-in")


def test_list_prints_each_connection_and_the_legacy_count_and_sends_the_secret_only_in_the_header(node, capsys):
    Node.answers[("GET", "/api/node/connections")] = [(200, {"connections": ROWS, "legacy_connectors": 2})]
    connections.show(node)
    out = capsys.readouterr().out
    assert "aaaaaaaa  Notes" in out and "not signed in" in out and "needs attention (expired)" in out
    assert "2 connector(s) added before connections" in out and SECRET not in out
    method, path, headers, body = Node.seen[0]
    assert (method, path, body) == ("GET", "/api/node/connections", None)
    assert headers["astral-node-operator"] == SECRET and "authorization" not in headers
    assert SECRET not in node.scripts[0][1] and node.scripts[0][0] == "node"


def test_an_empty_list_says_how_to_add_one(node, capsys):
    Node.answers[("GET", "/api/node/connections")] = [(200, {"connections": [], "legacy_connectors": "x"})]
    connections.show(node)
    assert "no connections yet" in capsys.readouterr().out


def test_a_connection_is_found_by_name_id_or_prefix(node):
    rows = connections.list_connections(node)[0]
    assert connections.resolve(rows, "Search")["id"] == ID_B
    assert connections.resolve(rows, ID_A)["id"] == ID_A
    assert connections.resolve(rows, "bbbb")["id"] == ID_B
    for key, text in (("nothing", "no connection"), ("b", "no connection")):
        with pytest.raises(connections.ConnectionsError, match=text):
            connections.resolve(rows, key)
    twins = [{"id": "c" * 31 + "1"}, {"id": "c" * 31 + "2"}]
    with pytest.raises(connections.ConnectionsError, match="matches 2"):
        connections.resolve(twins, "cccc")


def test_a_pasted_sign_in_posts_the_address_exactly_and_prints_no_code(node, capsys):
    Node.answers[start_path()] = [(200, {"flow": "f1", "authorization_url": "https://auth.test/authorize?state=s"})]
    done = "/api/node/connections/sign-in/f1/complete"
    Node.answers[("POST", done)] = [(200, {"state": "signed_in", "reason": None})]
    pasted = "https://astraldeep.github.io/oauth/callback?code=SECRETCODE&state=s"
    connections.sign_in(node, "Notes", paste=True, read=lambda: pasted)
    out = capsys.readouterr().out
    assert posts() == [(start_path()[1], {"redirect": "pasted"}), (done, {"redirect_url": pasted})]
    assert "https://auth.test/authorize?state=s" in out and "signed in: Notes" in out
    assert "SECRETCODE" not in out and SECRET not in out


def test_a_pasted_address_that_is_not_https_is_refused_before_the_node_hears_it(node):
    Node.answers[start_path()] = [(200, {"flow": "f1", "authorization_url": "https://auth.test/a"})]
    with pytest.raises(connections.ConnectionsError, match="full https"):
        connections.sign_in(node, "Notes", paste=True, read=lambda: "http://evil.test/?code=x")
    assert [p for p, _ in posts()] == [start_path()[1]]


def test_a_sign_in_that_does_not_end_signed_in_is_an_error(node):
    Node.answers[start_path()] = [(200, {"flow": "f1", "authorization_url": "https://auth.test/a"})]
    Node.answers[("POST", "/api/node/connections/sign-in/f1/complete")] = [
        (200, {"state": "needs_attention", "reason": "refused"})]
    with pytest.raises(connections.ConnectionsError, match=r"not signed in \(refused\)"):
        connections.sign_in(node, "Notes", paste=True, read=lambda: "https://x.test/cb?code=1")


def test_the_loopback_listener_captures_the_browser_redirect_and_posts_it(node, capsys):
    Node.answers[start_path()] = [(200, {"flow": "f2", "authorization_url": "https://auth.test/authorize?x=1"})]
    done = "/api/node/connections/sign-in/f2/complete"
    Node.answers[("POST", done)] = [(200, {"state": "signed_in"})]
    seen = []

    def out(text):
        seen.append(text)
        if "authorize" in text:
            port = Node.seen[-1][3]["port"]
            threading.Thread(target=lambda: urllib.request.urlopen(
                f"http://127.0.0.1:{port}/callback?code=SECRETCODE&state=s2").read()).start()

    connections.sign_in(node, "Notes", out=out)
    begin, finish = posts()
    port = begin[1]["port"]
    assert begin[1] == {"redirect": "loopback", "port": port} and port > 0
    assert finish == (done, {"redirect_url": f"http://127.0.0.1:{port}/callback?code=SECRETCODE&state=s2"})
    assert "SECRETCODE" not in "\n".join(seen) and SECRET not in "\n".join(seen)


def test_the_listener_ignores_other_paths_and_foreign_hosts():
    listener = connections.Listener()
    try:
        results = []

        def knock(path, host):
            connection = http.client.HTTPConnection("127.0.0.1", listener.port, timeout=5)
            connection.request("GET", path, headers={"Host": host})
            results.append(connection.getresponse().status)

        for path, host in (("/other?code=1", f"127.0.0.1:{listener.port}"),
                           ("/callback?code=1", f"evil.test:{listener.port}")):
            thread = threading.Thread(target=knock, args=(path, host))
            thread.start()
            listener.handle_request()
            thread.join()
        assert results == [404, 400] and listener.captured is None
        ticks = iter([0, 0, 1000])
        with pytest.raises(connections.ConnectionsError, match="no sign-in came back"):
            listener.wait(10, clock=lambda: next(ticks))
    finally:
        listener.server_close()


def test_the_first_callback_wins_and_a_busy_port_is_explained(node):
    listener = connections.Listener()
    listener.captured = "http://127.0.0.1:1/callback?code=first"
    thread = threading.Thread(target=lambda: urllib.request.urlopen(
        f"http://127.0.0.1:{listener.port}/callback?code=second").read())
    thread.start()
    listener.handle_request()
    thread.join()
    assert listener.wait(5) == "http://127.0.0.1:1/callback?code=first"
    with pytest.raises(connections.ConnectionsError, match="cannot listen"):
        connections.sign_in(node, "Notes", port=listener.port)
    listener.server_close()


def test_the_device_code_is_the_only_thing_printed_and_polling_never_beats_seven_seconds(node, capsys):
    Node.answers[start_path()] = [(200, {"flow": "f3", "user_code": "WDJB-MJHT", "interval": 2, "expires_in": 600,
                                         "verification_uri": "https://auth.test/device"})]
    poll = ("POST", "/api/node/connections/sign-in/f3/poll")
    Node.answers[poll] = [(200, {"status": "authorization_pending"}),
                          (429, {"detail": "node.rate_limited", "retry-after": "30"}),
                          (200, {"status": "slow_down"}), (200, {"status": "signed_in"})]
    waits = []
    now = iter(range(0, 1000, 10))
    connections.sign_in(node, "Notes", use_device_code=True, sleep=waits.append, clock=lambda: next(now))
    out = capsys.readouterr().out
    assert posts()[0] == (start_path()[1], {"redirect": "device_code"})
    assert [body for _, body in posts()[1:]] == [{}, {}, {}, {}]
    assert waits == [7, 7, 30, 37] and "WDJB-MJHT" in out and "https://auth.test/device" in out and SECRET not in out
    assert deletes() == [f"/api/node/connections/{ID_A}/sign-in/f3"]


@pytest.mark.parametrize("status,text", [("access_denied", "access_denied"), ("expired_token", "expired_token")])
def test_a_denied_or_expired_device_code_stops_and_cancels_the_flow(node, status, text):
    Node.answers[start_path()] = [(200, {"flow": "f4", "user_code": "ABCD", "verification_uri": "https://a.test/d"})]
    Node.answers[("POST", "/api/node/connections/sign-in/f4/poll")] = [(200, {"status": status})]
    with pytest.raises(connections.ConnectionsError, match=text):
        connections.sign_in(node, "Notes", use_device_code=True, sleep=lambda s: None)
    assert deletes() == [f"/api/node/connections/{ID_A}/sign-in/f4"]


def test_a_device_code_that_runs_out_of_time_or_polls_into_a_refusal_stops(node):
    Node.answers[start_path()] = [(200, {"flow": "f5", "user_code": "ABCD", "interval": 1, "expires_in": 5,
                                         "verification_uri": "https://a.test/d"})]
    poll = ("POST", "/api/node/connections/sign-in/f5/poll")
    Node.answers[poll] = [(200, {"status": "authorization_pending"})]
    now = iter(range(0, 100, 3))
    with pytest.raises(connections.ConnectionsError, match="expired before"):
        connections.sign_in(node, "Notes", use_device_code=True, sleep=lambda s: None, clock=lambda: next(now))
    Node.answers[poll] = [(404, {"detail": "node.flow_unknown"})]
    with pytest.raises(connections.ConnectionsError, match="node.flow_unknown"):
        connections.sign_in(node, "Notes", use_device_code=True, sleep=lambda s: None)


@pytest.mark.parametrize("answer", [{"flow": "f", "user_code": "bad\ncode", "verification_uri": "https://a.test/d"},
                                    {"flow": "f", "user_code": "ABCD", "verification_uri": "http://a.test/d"},
                                    {"flow": "../x", "user_code": "ABCD", "verification_uri": "https://a.test/d"}])
def test_a_malformed_device_answer_is_refused(node, answer):
    Node.answers[start_path()] = [(200, answer)]
    with pytest.raises(connections.ConnectionsError, match="no usable device code|did not start"):
        connections.sign_in(node, "Notes", use_device_code=True, sleep=lambda s: None)


def test_an_unusable_sign_in_address_is_refused(node):
    Node.answers[start_path()] = [(200, {"flow": "f", "authorization_url": "javascript:alert(1)"})]
    with pytest.raises(connections.ConnectionsError, match="no usable sign-in address"):
        connections.sign_in(node, "Notes", paste=True, read=lambda: "")


def test_both_modes_at_once_is_refused(node):
    with pytest.raises(connections.ConnectionsError, match="choose one"):
        connections.sign_in(node, "Notes", paste=True, use_device_code=True)
    assert Node.seen == []


def test_node_refusals_are_explained_and_odd_details_never_echoed(node):
    path = ("GET", "/api/node/connections")
    for answer, text in (((403, {"detail": "node.loopback_required"}), "own machine"),
                         ((401, {"detail": "node.operator_secret_required"}), "operator secret"),
                         ((400, {"detail": "connection.unknown"}), "connection.unknown"),
                         ((500, {"detail": "\x1b[31mred secret=abc"}), "http.500"),
                         ((502, b"<html>not json</html>"), "http.502")):
        Node.answers[path] = [answer]
        with pytest.raises(connections.ConnectionsError, match=text):
            connections.list_connections(node)


def test_a_silent_node_says_so():
    class Silent:
        def python(self, *args, **kwargs):
            return subprocess.CompletedProcess([], 1, stdout="", stderr="boom")

    with pytest.raises(connections.ConnectionsError, match="did not answer"):
        connections.list_connections(Silent())


def test_a_missing_secret_file_is_sent_as_absent_and_the_secret_never_enters_the_script(node, tmp_path):
    (tmp_path / "operator-secret").unlink()
    with pytest.raises(connections.ConnectionsError, match="operator secret"):
        connections.list_connections(node)
    assert Node.seen[0][2]["astral-node-operator"] == "absent"
    assert "operator-secret" in node.scripts[0][1] and SECRET not in node.scripts[0][1]


def test_names_with_control_characters_are_printed_safely():
    line = connections.describe({"id": ID_A, "name": "a\x1b[2Jb", "state": "signed_in"})
    assert "\x1b" not in line and "signed in" in line
    assert "unsupported: connection.malformed" in connections.describe(
        {"id": ID_A, "name": "n", "status": "unsupported", "reason": "connection.malformed"})


def test_the_cli_parses_both_commands_and_refuses_both_modes(monkeypatch):
    called = []
    monkeypatch.setattr(operate, "connections_command", called.append)

    def run(*argv):
        monkeypatch.setattr("sys.argv", ["astralvessel", *argv])
        cli.main()

    run("connections", "list")
    run("connections", "sign-in", "Notes", "--device-code", "--deployment", "d.toml")
    run("connections", "sign-in", "Notes", "--paste", "--port", "9000")
    assert [a.connections_command for a in called] == ["list", "sign-in", "sign-in"]
    assert (called[1].device_code, called[1].paste, called[1].deployment) == (True, False, "d.toml")
    assert (called[2].paste, called[2].port) == (True, 9000)
    with pytest.raises(SystemExit):
        run("connections", "sign-in", "Notes", "--paste", "--device-code")


def test_the_command_reaches_the_connection_functions_and_turns_errors_into_exits(tmp_path, monkeypatch, capsys):
    path = operate.write_deployment(tmp_path, operate.Answers(), example=False)
    (tmp_path / "compose.generated.yml").write_text("services: {}\n")
    calls = []
    monkeypatch.setattr(connections, "show", lambda stack: calls.append(("list", stack)))
    monkeypatch.setattr(connections, "sign_in", lambda stack, key, **kw: calls.append((key, kw)))
    base = {"deployment": str(path), "paste": False, "device_code": False, "port": 0, "connection": "Notes"}
    operate.connections_command(argparse.Namespace(connections_command="list", **base))
    operate.connections_command(argparse.Namespace(connections_command="sign-in", **{**base, "paste": True}))
    assert calls[0][0] == "list" and calls[1] == ("Notes", {"paste": True, "use_device_code": False, "port": 0})

    def refuse(stack, key, **kw):
        raise connections.ConnectionsError("no connection is named x")

    monkeypatch.setattr(connections, "sign_in", refuse)
    with pytest.raises(SystemExit, match="no connection is named x"):
        operate.connections_command(argparse.Namespace(connections_command="sign-in", **base))


def test_check_reports_attention_and_legacy_connectors_and_deletes_nothing(node, tmp_path, capsys):
    Node.answers[("GET", "/api/node/connections")] = [(200, {"connections": ROWS, "legacy_connectors": 3})]
    before = sorted(p.name for p in tmp_path.iterdir())
    operate.connection_notes(node)
    out = capsys.readouterr().out
    assert ("ATTENTION  connection Search: needs attention (expired); "
            "run astralvessel connections sign-in bbbbbbbb") in out
    assert "Notes" not in out and "NOTE  node: 3 connector(s)" in out and "nothing was changed or deleted" in out
    assert sorted(p.name for p in tmp_path.iterdir()) == before and all(m == "GET" for m, *_ in Node.seen)


def test_check_does_not_fail_when_an_older_node_cannot_list_connections(node, capsys):
    Node.answers[("GET", "/api/node/connections")] = [(404, {"detail": "Not Found"})]
    operate.connection_notes(node)
    assert "NOTE  node: connections were not listed" in capsys.readouterr().out


def test_check_runs_the_notes_unless_only_isolation_is_checked(tmp_path, monkeypatch):
    path = operate.write_deployment(tmp_path, operate.Answers(), example=False)
    (tmp_path / "compose.generated.yml").write_text("services: {}\n")
    seen = []

    class Report:
        passed, failed, skipped = 1, [], 0

    monkeypatch.setattr(operate, "run_probes", lambda *a: Report)
    monkeypatch.setattr(operate, "node_checks", lambda *a: [])
    monkeypatch.setattr(operate, "_require_env", lambda *a: None)
    monkeypatch.setattr(operate, "connection_notes", lambda stack: seen.append(stack))
    operate.check(argparse.Namespace(deployment=str(path), isolation_only=True))
    assert seen == []
    operate.check(argparse.Namespace(deployment=str(path), isolation_only=False))
    assert len(seen) == 1


def test_a_bad_paste_a_failed_complete_and_a_listener_timeout_all_cancel_their_flow(node):
    Node.answers[start_path()] = [(200, {"flow": "f6", "authorization_url": "https://auth.test/a?state=s"})]
    with pytest.raises(connections.ConnectionsError, match="full https"):
        connections.sign_in(node, "Notes", paste=True, read=lambda: "nope")
    Node.answers[("POST", "/api/node/connections/sign-in/f6/complete")] = [(400, {"detail": "connection.iss_mismatch"})]
    with pytest.raises(connections.ConnectionsError, match="iss_mismatch"):
        connections.sign_in(node, "Notes", paste=True, read=lambda: "https://x.test/cb?code=1")

    class Quiet(connections.Listener):
        def wait(self, seconds, clock=None):
            raise connections.ConnectionsError("no sign-in came back in time; run the command again")

    with pytest.raises(connections.ConnectionsError, match="no sign-in came back"):
        connections.sign_in(node, "Notes", listener=Quiet)
    assert deletes() == [f"/api/node/connections/{ID_A}/sign-in/f6"] * 3


def test_a_cancel_the_node_refuses_does_not_hide_the_real_result(node, capsys):
    Node.answers[start_path()] = [(200, {"flow": "f7", "authorization_url": "https://auth.test/a"})]
    Node.answers[("POST", "/api/node/connections/sign-in/f7/complete")] = [(200, {"state": "signed_in"})]
    Node.answers[("DELETE", f"/api/node/connections/{ID_A}/sign-in/f7")] = [(404, {"detail": "node.flow_unknown"})]
    connections.sign_in(node, "Notes", paste=True, read=lambda: "https://x.test/cb?code=1")
    assert "signed in: Notes" in capsys.readouterr().out


def test_a_callback_with_the_wrong_state_is_ignored_until_the_right_one_arrives():
    listener = connections.Listener()
    listener.expected_state = "good"
    try:
        for query in ("code=1&state=evil", "code=1", "code=2&state=good"):
            thread = threading.Thread(target=lambda q=query: _knock(listener.port, "/callback?" + q))
            thread.start()
            listener.handle_request()
            thread.join()
            if query.endswith("evil") or query == "code=1":
                assert listener.captured is None
        assert listener.captured.endswith("/callback?code=2&state=good")
    finally:
        listener.server_close()


def _knock(port, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("GET", path)
    connection.getresponse().read()


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("docker", 60), FileNotFoundError(2, "no docker"),
                                     PermissionError(13, "denied")])
def test_a_container_call_that_times_out_or_cannot_start_is_a_connection_error(failure, tmp_path, monkeypatch, capsys):
    class Broken:
        def python(self, *args, **kwargs):
            raise failure

    with pytest.raises(connections.ConnectionsError):
        connections.list_connections(Broken())
    operate.connection_notes(Broken())
    assert "NOTE  node: connections were not listed" in capsys.readouterr().out


def test_a_wrong_operator_secret_is_refused(node, tmp_path):
    (tmp_path / "operator-secret").write_text("wrong\n")
    with pytest.raises(connections.ConnectionsError, match="operator secret"):
        connections.list_connections(node)


def test_the_pasted_address_is_read_from_standard_input_or_the_terminal(monkeypatch):
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO("https://x.test/cb?code=1\n"))
    assert connections.read_pasted() == "https://x.test/cb?code=1"

    class Terminal(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr(connections.getpass, "getpass", lambda prompt: "  https://y.test/cb  ")
    assert connections.read_pasted() == "https://y.test/cb"


def test_an_unknown_state_reads_as_unknown_and_ports_are_range_checked(monkeypatch):
    assert "unknown" in connections.describe({"id": ID_A, "name": "n", "state": "odd"})
    monkeypatch.setattr(operate, "connections_command", lambda args: None)
    for bad in ("65536", "-1", "x"):
        monkeypatch.setattr("sys.argv", ["astralvessel", "connections", "sign-in", "N", "--port", bad])
        with pytest.raises(SystemExit):
            cli.main()


def test_the_container_script_makes_the_call_without_a_proxy(node, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    node.env.update({"HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9"})
    assert connections.list_connections(node)[0][0]["id"] == ID_A
