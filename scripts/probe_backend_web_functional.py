#!/usr/bin/env python3
"""Exercises real provider/settings/chat flows in the isolated qualification app through
the ordinary authenticated settings UI; keeps provider keys in observer memory only,
and never itself authorizes release.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import ssl
import sys
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from websockets.sync.client import connect

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_backend_web_auth import WEB, ProbeError, login, read_identities, require, session  # noqa: E402


MESSAGE = "Reply with the word ORBITAL exactly once. This is a synthetic release check. Do not use tools."
ATTACHMENT = b"The synthetic verification animal is a turquoise otter.\n"


def canonical_uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value, version=4)) == value
    except ValueError:
        return False


def approved_provider(path: Path, qualification_id: str):
    require(path.is_absolute() and path.resolve() == path and path.is_file() and not path.is_symlink()
            and 0 < path.stat().st_size <= 32768
            and (os.name == "nt" or not path.stat().st_mode & 0o077),
            "approved provider input must be a bounded private regular file")
    document = json.loads(path.read_text())
    require(isinstance(document, dict) and set(document) == {"qualification_id", "config"}
            and document["qualification_id"] == qualification_id, "provider approval belongs to another namespace")
    config = document["config"]
    require(isinstance(config, dict) and set(config) == {"provider", "base_url", "model", "api_key"}
            and all(isinstance(value, str) and len(value) <= 8192 for value in config.values())
            and all(config[key].strip() for key in ("provider", "base_url", "model")),
            "approved provider input has an invalid shape")
    endpoint = urlsplit(config["base_url"])
    require(endpoint.scheme == "https" and endpoint.hostname and not endpoint.username
            and not endpoint.password and not endpoint.query and not endpoint.fragment,
            "approved provider endpoint must use HTTPS without credentials or query parameters")
    return config


class BrowserWire:
    def __init__(self, socket, token, *, clock=time.monotonic):
        self.socket, self.clock = socket, clock
        self.generation = str(uuid4())
        self.socket.send(json.dumps({
            "type": "register_ui", "token": token, "capabilities": ["render", "stream"],
            "session_id": "qualification-" + str(uuid4()), "connection_generation": self.generation,
            "device": {"device_type": "browser", "screen_width": 1440, "screen_height": 1000,
                       "supports_charts": True, "supports_tables": True, "supports_images": True},
        }))
        self.until(lambda frame: frame.get("type") == "rote_config", timeout=45)

    def until(self, predicate, *, timeout):
        deadline = self.clock() + timeout
        frames = []
        while self.clock() < deadline:
            try:
                raw = self.socket.recv(timeout=max(0.001, deadline - self.clock()))
            except TimeoutError:
                raise ProbeError("application operation exceeded its acceptance deadline") from None
            require(isinstance(raw, str) and len(raw) <= 16 * 1024 * 1024, "application frame exceeded its bound")
            frame = json.loads(raw)
            require(isinstance(frame, dict), "application frame was malformed")
            require(frame.get("type") != "auth_required", "application rejected the authenticated socket")
            frames.append(frame)
            require(len(frames) <= 10000, "application frame count exceeded its bound")
            if predicate(frame):
                return frames
        raise ProbeError("application operation exceeded its acceptance deadline")

    def action(self, name, payload, *, chat_id=None, timeout=240):
        request, submission = str(uuid4()), str(uuid4())
        body = dict(payload) | {"submission_id": submission, "request_generation": request}
        frame = {"type": "ui_event", "action": name, "payload": body,
                 "submission_id": submission, "request_generation": request,
                 "connection_generation": self.generation}
        if chat_id is not None:
            require(canonical_uuid(chat_id), "chat identity was invalid")
            frame["session_id"] = chat_id
            body["chat_id"] = chat_id
        self.socket.send(json.dumps(frame))
        frames = self.until(lambda value: value.get("type") == "operation_status"
                            and value.get("request_generation") == request
                            and value.get("terminal") is True, timeout=timeout)
        terminal = frames[-1]
        require(terminal.get("connection_generation") == self.generation
                and terminal.get("action") == name and canonical_uuid(terminal.get("operation_id"))
                and terminal.get("chat_id") == chat_id, "operation terminal scope differed")
        require(terminal.get("state") == "completed" and terminal.get("error") is None,
                "application operation did not complete successfully")
        return frames


@contextmanager
def wire(client, context):
    current = session(client)
    require(current.get("authenticated") is True and current.get("user_id") == "fixture-owner-a"
            and current.get("access_token"), "functional probe lost its authenticated owner")
    client.headers["Authorization"] = "Bearer " + current["access_token"]
    with connect("wss://ad-bwq-web:9443/ws", ssl=context, origin=WEB,
                 open_timeout=20, close_timeout=5, max_size=16 * 1024 * 1024) as socket:
        yield BrowserWire(socket, current["access_token"])


def create_chat(client):
    result = client.post(WEB + "/api/chats", json={})
    require(result.status_code == 201 and canonical_uuid(result.json().get("chat_id")),
            "functional chat creation failed")
    return result.json()["chat_id"]


def retained_chat(client, chat_id, expected):
    response = client.get(WEB + "/api/chats/" + chat_id)
    require(response.status_code == 200, "completed chat could not be restored")
    document = response.json()
    rows = document.get("messages", [])
    require(any(row.get("role") == "assistant" and expected.casefold() in str(row.get("content", "")).casefold()
                for row in rows), "provider response was absent from retained history")
    return document


def exercise(owner, config, qualification_id, *, open_wire):
    with open_wire() as browser:
        browser.action("chrome_llm_save", {"fields": dict(config) | {"data_sharing_acknowledged": True}}, timeout=90)
    chat_id = create_chat(owner)
    with open_wire() as browser:
        frames = browser.action("chat_message", {"message": MESSAGE, "snapshot_purpose": "commit"}, chat_id=chat_id)
    retained_chat(owner, chat_id, "ORBITAL")
    snapshots = [frame for frame in frames if frame.get("type") == "conversation_snapshot"
                 and frame.get("chat_id") == chat_id and frame.get("snapshot_purpose") == "commit"]
    require(bool(snapshots), "chat completed without a durable conversation snapshot")
    with open_wire() as browser:
        restored = browser.action("load_chat", {"snapshot_purpose": "hydration"}, chat_id=chat_id)
    hydrated = [frame for frame in restored if frame.get("type") == "conversation_snapshot"
                and frame.get("chat_id") == chat_id and frame.get("snapshot_purpose") == "hydration"]
    require(bool(hydrated) and hydrated[-1].get("transcript") == snapshots[-1].get("transcript")
            and hydrated[-1].get("canvas") == snapshots[-1].get("canvas"),
            "reconnected workspace differs from committed conversation state")

    upload = owner.post(WEB + "/api/upload", files={"file": ("synthetic-animal.txt", ATTACHMENT, "text/plain")})
    require(upload.status_code == 201 and upload.json().get("sha256") == hashlib.sha256(ATTACHMENT).hexdigest(),
            "functional attachment upload failed")
    attachment = upload.json()
    require(canonical_uuid(attachment.get("attachment_id")), "functional attachment identity was invalid")
    attachment_chat = create_chat(owner)
    with open_wire() as browser:
        browser.action("chat_message", {
            "message": "Read the attached synthetic file. Which animal and color does it name?",
            "snapshot_purpose": "commit", "attachments": [{
                "attachment_id": attachment["attachment_id"], "filename": "synthetic-animal.txt", "category": "document",
            }],
        }, chat_id=attachment_chat)
    retained_chat(owner, attachment_chat, "turquoise")
    retained_chat(owner, attachment_chat, "otter")

    audit = owner.get(WEB + "/api/audit", params={"limit": 200})
    require(audit.status_code == 200, "authenticated audit view failed")
    entries = audit.json().get("items", [])
    require(any(item.get("action_type") == "llm_config_change" for item in entries),
            "provider settings produced no retained audit event")
    return {"schema_version": 1, "qualification_id": qualification_id,
            "provider_settings_save_and_probe": "passed", "real_provider_chat": "passed",
            "workspace_reconnection": "passed", "attachment_extraction_in_chat": "passed",
            "provider_audit_observed": "passed", "synthetic_content_only": True,
            "release_authorized": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qualification-id", required=True)
    for name in ("material-root", "approved-provider", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        require(not args.output.exists() and not args.output.is_symlink(), "evidence output must be new")
        identities = read_identities(args.material_root, args.qualification_id)
        config = approved_provider(args.approved_provider, args.qualification_id)
        context = ssl.create_default_context(cafile=args.material_root / "app/ca-bundle.pem")
        with httpx.Client(verify=context, trust_env=False, timeout=20, follow_redirects=False) as owner:
            login(owner, "owner-a", identities["owner-a"], args.qualification_id)
            report = exercise(owner, config, args.qualification_id, open_wire=lambda: wire(owner, context))
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(report, stream, sort_keys=True, indent=2)
            stream.write("\n")
        print("Synthetic provider and application functional probe passed")
        return 0
    except ProbeError as exc:
        print("Synthetic functional probe failed: " + str(exc))
        return 1
    except Exception:
        print("Synthetic functional probe failed; no provider or session values were retained")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
