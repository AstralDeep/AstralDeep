#!/usr/bin/env python3
"""Exercises real browser login/session flows against a synthetic namespace to produce
acceptance evidence — no mock auth, DB writes, or credentials enter evidence; this is
one acceptance producer, not a release decision.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import ssl
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx


WEB = "https://ad-bwq-web:9443"
IAM = "https://ad-bwq-keycloak:8443"


class ProbeError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ProbeError(message)


class LoginForm(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.action = None
        self.fields = {}
        self.inside = False
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.inside = attrs.get("id") == "kc-form-login"
            if self.inside:
                self.action = attrs.get("action")
        if self.inside and tag == "input" and attrs.get("type") == "hidden" and attrs.get("name"):
            self.fields[attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False


def bound_url(value: str, origin: str, path_prefix: str) -> str:
    parsed = urlsplit(value)
    expected = urlsplit(origin)
    require(parsed.scheme == "https" and parsed.netloc == expected.netloc
            and not parsed.username and not parsed.password and not parsed.fragment
            and parsed.path.startswith(path_prefix), "authentication redirect left its synthetic origin")
    return value


def session(client):
    response = client.get(WEB + "/auth/session")
    require(response.status_code == 200, "session endpoint failed")
    value = response.json()
    require(isinstance(value, dict), "session response was malformed")
    return value


def login(client, username: str, password: str, qualification_id: str):
    require(session(client).get("authenticated") is False, "new browser was already authenticated")
    start = client.get(WEB + "/auth/login?next=%2F")
    require(start.status_code in {302, 303, 307}, "application did not start interactive login")
    authority_path = f"/realms/astral-bwq-{qualification_id}/"
    authorization = bound_url(start.headers.get("location", ""), IAM, authority_path)
    query = parse_qs(urlsplit(authorization).query)
    require(query.get("code_challenge_method") == ["S256"] and query.get("code_challenge")
            and query.get("state"), "application login lacks state or PKCE")
    page = client.get(authorization)
    require(page.status_code == 200, "Keycloak login form was unavailable")
    form = LoginForm(page.text)
    require(form.action is not None, "Keycloak login form was missing")
    action = bound_url(form.action, IAM, authority_path)
    authenticated = client.post(action, data=form.fields | {"username": username, "password": password})
    require(authenticated.status_code in {302, 303}, "real Keycloak login failed")
    callback = bound_url(authenticated.headers.get("location", ""), WEB, "/auth/callback")
    require(urlsplit(callback).path == "/auth/callback", "callback path differed")
    exchanged = client.get(callback)
    require(exchanged.status_code in {302, 303, 307}
            and urljoin(WEB, exchanged.headers.get("location", "")) == WEB + "/",
            "application callback did not establish a session")
    current = session(client)
    require(current.get("authenticated") is True and current.get("user_id") == "fixture-" + username
            and bool(current.get("access_token")), "application session owner differed")
    client.headers["Authorization"] = "Bearer " + current["access_token"]
    return current


def exercise(clients, identities, qualification_id, *, clock=time.monotonic, wait=time.sleep):
    owner, foreign = clients
    current = login(owner, "owner-a", identities["owner-a"], qualification_id)
    login(foreign, "owner-b", identities["owner-b"], qualification_id)
    bad_callback = owner.get(WEB + "/auth/callback?code=invalid&state=unissued")
    require(bad_callback.status_code == 200 and "invalid callback" in bad_callback.text
            and "Sign-in problem" in bad_callback.text, "unissued login state was accepted")
    require(session(owner).get("user_id") == "fixture-owner-a", "forged callback changed the browser owner")

    created = owner.post(WEB + "/api/chats", json={})
    require(created.status_code == 201, "authenticated chat creation failed")
    chat_id = created.json().get("chat_id", "")
    require(re.fullmatch(r"[0-9a-f-]{36}", chat_id), "chat identity was invalid")
    path = WEB + "/api/chats/" + chat_id
    require(owner.get(path).status_code == 200, "owner could not restore chat")
    require(foreign.get(path).status_code in {403, 404}, "foreign owner could read chat")
    require(foreign.get(path + "/steps").status_code in {403, 404}, "foreign owner could read steps")
    own_list = owner.get(WEB + "/api/chats")
    foreign_list = foreign.get(WEB + "/api/chats")
    require(own_list.status_code == foreign_list.status_code == 200, "history listing failed")
    require(chat_id in json.dumps(own_list.json()) and chat_id not in json.dumps(foreign_list.json()),
            "history listing crossed ownership")

    contents = b"Synthetic backend/browser qualification attachment.\n"
    uploaded = owner.post(WEB + "/api/upload", files={"file": ("qualification.txt", contents, "text/plain")})
    require(uploaded.status_code == 201, "authenticated attachment upload failed")
    attachment = uploaded.json()
    attachment_id = attachment.get("attachment_id", "")
    require(re.fullmatch(r"[0-9a-f-]{36}", attachment_id)
            and attachment.get("sha256") == hashlib.sha256(contents).hexdigest()
            and attachment.get("size_bytes") == len(contents), "attachment identity or retained bytes differed")
    attachment_path = WEB + "/api/attachments/" + attachment_id
    restored = owner.get(attachment_path)
    require(restored.status_code == 200 and restored.json().get("sha256") == attachment["sha256"],
            "attachment metadata did not survive restoration")
    require(foreign.get(attachment_path).status_code == 404, "foreign owner could read attachment")
    require(foreign.delete(attachment_path).status_code == 404, "foreign owner could delete attachment")
    require(owner.get(attachment_path).status_code == 200, "foreign deletion changed retained attachment")
    foreign_files = foreign.get(WEB + "/api/attachments")
    require(foreign_files.status_code == 200 and attachment_id not in json.dumps(foreign_files.json()),
            "attachment listing crossed ownership")

    # Never shortcut TTL/session; poll past the real 90s expiry
    deadline = clock() + 150
    renewed = False
    while clock() < deadline:
        observed = session(owner)
        require(observed.get("authenticated") is True and observed.get("user_id") == "fixture-owner-a",
                "session renewal lost its authenticated owner")
        if observed.get("access_token") and observed["access_token"] != current["access_token"]:
            renewed = True
            break
        wait(2)
    require(renewed, "normal application session renewal did not occur")
    logout = owner.post(WEB + "/auth/logout")
    require(logout.status_code in {200, 302, 303, 307}, "logout failed")
    owner.headers.pop("Authorization", None)
    require(session(owner).get("authenticated") is False, "logout retained an authenticated session")
    require(owner.get(path).status_code in {401, 403}, "signed-out browser retained chat access")
    require(session(foreign).get("authenticated") is True, "one owner's logout revoked another owner")
    return {"schema_version": 1, "qualification_id": qualification_id,
            "real_application_keycloak_login_pkce": "passed", "login_state_denial": "passed",
            "application_session_renewal": "passed", "chat_history_retention": "passed",
            "attachment_upload_metadata_retention": "passed", "attachment_owner_denials": "passed",
            "owner_read_denials": "passed", "logout_isolation": "passed",
            "synthetic_only": True, "release_authorized": False}


def read_identities(material: Path, qualification_id: str):
    require(re.fullmatch(r"[0-9a-f]{32}", qualification_id), "qualification identity is invalid")
    require(material.is_absolute() and material.is_dir() and not material.is_symlink(),
            "material root must be a private directory")
    require(not (material / "app").is_symlink(), "material directory cannot redirect outside its namespace")
    for name in ("test-identities.json", "app/qualification.json", "app/ca-bundle.pem"):
        path = material / name
        require(path.is_file() and not path.is_symlink()
                and (os.name == "nt" or not path.stat().st_mode & 0o077), "material must be private regular files")
    marker = json.loads((material / "app/qualification.json").read_text())
    require(marker == {"schema_version": 1, "qualification_id": qualification_id, "classification": "synthetic"},
            "material does not belong to this synthetic namespace")
    identities = json.loads((material / "test-identities.json").read_text())["users"]
    require(set(identities) == {"owner-a", "owner-b", "administrator"}
            and all(isinstance(value, str) and len(value) >= 20 for value in identities.values()),
            "synthetic identities are incomplete")
    return identities


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qualification-id", required=True)
    parser.add_argument("--material-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        require(not args.output.exists() and not args.output.is_symlink(), "evidence output must be new")
        identities = read_identities(args.material_root, args.qualification_id)
        context = ssl.create_default_context(cafile=args.material_root / "app/ca-bundle.pem")
        with (httpx.Client(verify=context, trust_env=False, timeout=20, follow_redirects=False) as owner,
              httpx.Client(verify=context, trust_env=False, timeout=20, follow_redirects=False) as foreign):
            result = exercise((owner, foreign), identities, args.qualification_id)
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(result, stream, sort_keys=True, indent=2)
            stream.write("\n")
        print("Synthetic application authentication and ownership probe passed")
        return 0
    except ProbeError as exc:
        print("Synthetic application authentication probe failed: " + str(exc))
        return 1
    except Exception:
        print("Synthetic application authentication probe failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
