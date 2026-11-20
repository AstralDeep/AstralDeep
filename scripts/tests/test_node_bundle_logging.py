"""Checks secret redaction and size rotation of the node log."""

import errno
import os
import time

from node_bundle_support import node_logging as nl


def test_secret_values_come_only_from_secret_looking_names_and_skip_short_values():
    env = {"WEB_SESSION_ENC_KEY": "abcdef123456", "AGENT_API_KEY": "xy", "KEYCLOAK_CLIENT_ID": "astral", "AUDIT_HMAC_SECRET": "hmac-secret-value"}
    assert nl.secret_values(env) == ["hmac-secret-value", "abcdef123456"]


def test_known_values_bearer_tokens_jwts_and_assignments_are_removed():
    secrets = ["abcdef123456"]
    jwt = "eyJhbGciOiJFZERTQSJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJl"
    line = f"key abcdef123456 Authorization: Bearer {jwt} token={jwt} password: 'hunter2hunter2' api_key=zzz9999 ok=1"
    cleaned = nl.redact(line, secrets)
    for leaked in ("abcdef123456", jwt, "hunter2hunter2", "zzz9999"):
        assert leaked not in cleaned
    assert "ok=1" in cleaned and "Bearer [redacted]" in cleaned


def test_ordinary_lines_pass_through_unchanged():
    line = "INFO:     127.0.0.1:58481 - GET /healthz HTTP/1.1 200 OK"
    assert nl.redact(line, []) == line


def test_the_file_is_private_and_rotates_with_a_bounded_number_of_backups(tmp_path):
    sink = nl.RotatingFile(tmp_path / "logs", "node.log", max_bytes=40, backups=2)
    for index in range(12):
        sink.write(f"line number {index:03d} padding\n")
    names = sorted(path.name for path in (tmp_path / "logs").iterdir())
    assert names == ["node.log", "node.log.1", "node.log.2"]
    assert oct((tmp_path / "logs" / "node.log").stat().st_mode & 0o777) == "0o600"
    assert oct((tmp_path / "logs").stat().st_mode & 0o777) == "0o700"
    assert "line number 011" in (tmp_path / "logs" / "node.log").read_text()


def test_capture_output_redacts_what_the_process_writes_to_its_descriptors(tmp_path):
    pid = os.fork()
    if pid == 0:
        try:
            nl.capture_output(tmp_path / "logs", ["topsecretvalue"])
            os.write(1, b"started with topsecretvalue\n")
            os.write(2, b"warning without secrets\n")
            time.sleep(0.4)
        finally:
            os._exit(0)
    os.waitpid(pid, 0)
    text = (tmp_path / "logs" / "node.log").read_text()
    assert "started with [redacted]" in text and "warning without secrets" in text
    assert "topsecretvalue" not in text



def test_log_pump_redacts_buffered_input_without_replacing_test_descriptors(tmp_path, monkeypatch):
    read_end, write_end = os.pipe()
    os.write(write_end, b"token=synthetic-secret ordinary output\n")
    duplicated = []
    monkeypatch.setattr(nl.os, "pipe", lambda: (read_end, write_end))
    monkeypatch.setattr(nl.os, "dup2", lambda source, target: duplicated.append((source, target)))

    class ImmediateThread:
        def __init__(self, *, target, name, daemon):
            assert name == "node-log" and daemon is True
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(nl.threading, "Thread", ImmediateThread)
    sink = nl.capture_output(tmp_path / "logs", ["synthetic-secret"])
    try:
        assert duplicated == [(write_end, 1), (write_end, 2)]
        text = sink.path.read_text()
        assert "synthetic-secret" not in text and "ordinary output" in text
        assert "[redacted]" in text
    finally:
        sink.handle.close()


class FlakyHandle:
    def __init__(self, real):
        self.real = real

    def tell(self):
        return self.real.tell()

    def write(self, text):
        if "bad" in text:
            raise OSError(errno.ENOSPC, "no space")
        return self.real.write(text)

    def close(self):
        self.real.close()


def test_a_failing_log_write_is_recorded_once_by_code_and_the_pump_keeps_draining(tmp_path, monkeypatch):
    read_end, write_end = os.pipe()
    os.write(write_end, b"first\nbad one\nbad two\nlast\n")
    real_open = nl.RotatingFile._open
    monkeypatch.setattr(nl.RotatingFile, "_open", lambda self: FlakyHandle(real_open(self)))
    monkeypatch.setattr(nl.os, "pipe", lambda: (read_end, write_end))
    monkeypatch.setattr(nl.os, "dup2", lambda source, target: None)

    class ImmediateThread:
        def __init__(self, *, target, name, daemon):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(nl.threading, "Thread", ImmediateThread)
    sink = nl.capture_output(tmp_path / "logs", [])
    sink.handle.close()
    text = sink.path.read_text()
    assert sink.failures == ["ENOSPC"]
    assert "first" in text and "last" in text and "bad" not in text.replace("[log write failed", "")
    assert text.count("[log write failed: ENOSPC]") == 1


def test_a_failed_rotation_is_recorded_and_writing_resumes_on_the_next_line(tmp_path, monkeypatch):
    sink = nl.RotatingFile(tmp_path / "logs", "node.log", max_bytes=10, backups=2)
    sink.write("12345678\n")
    real_replace = os.replace
    calls = []

    def replace(source, target):
        calls.append(source)
        if len(calls) == 1:
            raise OSError(errno.EACCES, "denied")
        real_replace(source, target)

    monkeypatch.setattr(nl.os, "replace", replace)
    sink.write("abcdefgh\n")
    assert sink.failures == ["EACCES"] and sink.handle is None
    sink.write("recovered\n")
    sink.handle.close()
    assert "recovered" in sink.path.read_text()
