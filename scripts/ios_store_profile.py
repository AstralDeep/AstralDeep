"""Prepares an exact-certificate iPhone App Store profile with production push entitlement.
The protected upload job supplies its existing keychain and Apple API key; Watch profiles remain unchanged."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from ios_store_upload import API, BUNDLE, profile_identity, require


class ProvisioningError(ValueError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise ProvisioningError("Apple provisioning API redirects are refused")


def request_class(url, method):
    parts = urllib.parse.urlsplit(url).path.removeprefix("/v1/").split("/")
    resources = {"profiles", "bundleIds", "certificates", "bundleIdCapabilities"}
    resource = parts[0] if parts[0] in resources else "[redacted]"
    if len(parts) > 1:
        resource += "/{id}"
    if len(parts) > 2:
        resource += "/" + (parts[2] if parts[2] in resources else "[redacted]")
    return {"method": method, "resource": resource}


def safe_apple_error(error):
    safe = {"status": error.code, "code": "[redacted]", "title": "[redacted]", "detail": "[redacted]"}
    try:
        raw = error.read(64 * 1024 + 1)
        require(len(raw) <= 64 * 1024, "Apple error response exceeds bounds")
        errors = json.loads(raw).get("errors", [])
        first = errors[0] if errors else {}
        public_parameters = ("filter[identifier]", "filter[certificateType]", "filter[profileType]",
                             "filter[profileState]", "filter[name]", "limit", "include", "sort")
        parameter = first.get("source", {}).get("parameter")
        if parameter is not None:
            safe["source"] = {"parameter": parameter if parameter in public_parameters else "[redacted]"}
        prefixes = {"FORBIDDEN", "FORBIDDEN_ERROR", "UNAUTHORIZED", "UNAUTHORIZED_ERROR", "PARAMETER_ERROR",
                    "ENTITY_ERROR", "STATE_ERROR", "NOT_FOUND", "NOT_FOUND_ERROR", "RATE_LIMIT_EXCEEDED", "INTERNAL_ERROR"}
        suffixes = {"INVALID", "MISSING", "UNKNOWN", "NOT_ALLOWED", "INVALID_FILTER", "INVALID_FILTER_NAME",
                    "INVALID_FILTER_VALUE", "INVALID_PARAMETER", "INVALID_PARAMETER_VALUE", "UNKNOWN_PARAMETER",
                    "UNSUPPORTED_PARAMETER", "INVALID_QUERY_PARAMETER", "REQUIRED_AGREEMENTS_MISSING"}
        code = first.get("code", "")
        components = code.split(".") if isinstance(code, str) else []
        if components and components[0] in prefixes and all(item in suffixes for item in components[1:]):
            safe["code"] = code
        public_messages = {"Access Forbidden", "Forbidden", "This request is forbidden for security reasons.",
                           "A required agreement is missing or has expired.", "The API key in use does not allow this request.",
                           "A parameter has an invalid value", "A parameter is not supported", "A required parameter is missing",
                           "An unexpected query parameter was provided", "The request is invalid"}
        for field in ("title", "detail"):
            if first.get(field) in public_messages:
                safe[field] = first[field]
        detail = first.get("detail", "")
        if (safe["detail"] == "[redacted]" and isinstance(detail, str)
                and (str(safe["code"]).startswith("PARAMETER_ERROR")
                     or (error.code == 400 and any(word in detail.lower() for word in ("parameter", "filter", "query"))))):
            lowered = detail.lower()
            reason = next((label for phrases, label in (
                (("not supported", "unsupported", "unexpected query parameter", "unknown query parameter", "not allowed"),
                 "Unsupported parameter"),
                (("invalid", "not valid"), "Invalid parameter value"),
                (("missing", "required parameter"), "Missing required parameter"),
            ) if any(phrase in lowered for phrase in phrases)), None)
            if reason:
                names = [name for name in public_parameters if name in detail]
                safe["detail"] = reason + (": " + ", ".join(names) if names else "") + "; values redacted"
    except (ValueError, TypeError, AttributeError, IndexError):
        pass
    return safe


class AppleAPI:
    def __init__(self, key_path):
        from jose import jwt

        now = int(time.time())
        self.token = jwt.encode(
            {"iss": os.environ["ASC_ISSUER_ID"], "iat": now, "exp": now + 600,
             "aud": "appstoreconnect-v1"}, key_path.read_text(), algorithm="ES256",
            headers={"kid": os.environ["ASC_KEY_ID"]},
        )
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, url, payload=None):
        require(url.startswith(API), "Apple provisioning URL is outside the approved origin")
        if payload is not None:
            endpoint = url.removeprefix(API)
            require(endpoint in {"profiles", "bundleIdCapabilities"}, "Apple provisioning mutation refused")
            data = payload["data"]
            if endpoint == "profiles":
                require(data["type"] == "profiles" and data["attributes"]["profileType"] == "IOS_APP_STORE"
                        and set(data["relationships"]) == {"bundleId", "certificates"}
                        and len(data["relationships"]["certificates"]["data"]) == 1,
                        "only single-certificate iOS App Store profile creation is allowed")
            else:
                require(data["type"] == "bundleIdCapabilities"
                        and data["attributes"] == {"capabilityType": "PUSH_NOTIFICATIONS"}
                        and set(data["relationships"]) == {"bundleId"},
                        "only enabling the bound push capability is allowed")
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=body, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
        })
        operation = request_class(url, request.get_method())
        print(json.dumps({"apple_provisioning_request": operation}), flush=True)
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read(8 * 1024**2 + 1)
        except urllib.error.HTTPError as error:
            safe = safe_apple_error(error)
            print(json.dumps({"apple_provisioning_error": safe, **operation}), flush=True)
            if error.code == 403:
                raise ProvisioningError(
                    f"Apple API HTTP 403 ({safe['code']}): provisioning access denied; "
                    "check API key role, Certificates/Identifiers/Profiles access, and required developer agreements. "
                    "An authorized Account Holder or Admin must resolve access or supply the matching production-push profile",
                ) from None
            raise ProvisioningError(
                f"Apple provisioning API {operation['method']} {operation['resource']} HTTP {error.code}; no automatic retry",
            ) from None
        require(len(raw) <= 8 * 1024**2, "Apple provisioning API response exceeds its size limit")
        result = json.loads(raw)
        require(isinstance(result, dict) and "data" in result, "invalid Apple provisioning API response")
        return result

    def pages(self, path, params=None):
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        records = []
        seen = set()
        while url:
            require(url not in seen and len(seen) < 25, "Apple provisioning pagination exceeds bounds")
            seen.add(url)
            result = self.request(url)
            require(isinstance(result["data"], list), "invalid Apple provisioning list response")
            records.extend(result["data"])
            url = result.get("links", {}).get("next")
        return records

    def create(self, path, data):
        return self.request(API + path, {"data": data})["data"]

    def probe_profiles(self):
        result = self.request(API + "profiles?limit=1")
        require(isinstance(result["data"], list), "invalid Apple provisioning access response")


def signing_certificate(keychain, team):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509.oid import NameOID

    identities = subprocess.check_output(
        ["security", "find-identity", "-v", "-p", "codesigning", str(keychain)], text=True,
    )
    usable = set(re.findall(r'\b([0-9A-Fa-f]{40})\s+"Apple Distribution:', identities))
    pem = subprocess.check_output(["security", "find-certificate", "-a", "-p", str(keychain)])
    matches = []
    for block in re.findall(b"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", pem, re.DOTALL):
        cert = x509.load_pem_x509_certificate(block)
        names = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        teams = cert.subject.get_attributes_for_oid(NameOID.ORGANIZATIONAL_UNIT_NAME)
        if (cert.fingerprint(hashes.SHA1()).hex().upper() in {value.upper() for value in usable}
                and len(names) == len(teams) == 1 and names[0].value.startswith("Apple Distribution:")
                and teams[0].value == team):
            matches.append(cert.public_bytes(serialization.Encoding.DER))
    require(len(matches) == 1, "exactly one usable Apple Distribution identity for the configured team is required")
    return matches[0]


def decode_profile(payload, directory):
    require(isinstance(payload, bytes) and 0 < len(payload) <= 1024**2, "invalid provisioning profile size")
    descriptor, name = tempfile.mkstemp(prefix="astral-profile-check-", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
        return plistlib.loads(subprocess.check_output(
            ["security", "cms", "-D", "-i", name], stderr=subprocess.DEVNULL,
        ))
    finally:
        Path(name).unlink()


def bound_profile(profile, name, team, certificate):
    require(isinstance(name, str) and 0 < len(name) <= 100
            and all(ord(character) >= 32 and ord(character) != 127 for character in name),
            "profile name is invalid for a runner environment override")
    profile_identity(profile, BUNDLE, name, team)
    require("iOS" in profile.get("Platform", []), "iPhone signing requires an iOS-family profile")
    require(profile.get("DeveloperCertificates") == [certificate],
            "iPhone profile must contain exactly the imported distribution certificate")
    require(profile["Entitlements"].get("com.apple.developer.team-identifier") == team,
            "iPhone profile team entitlement mismatch")
    return profile["Entitlements"].get("aps-environment") == "production"


def resource_id(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9-]{1,100}", value),
            "invalid Apple provisioning resource identity")
    return value


def renewed_profile(api, certificate, team, directory):
    api.probe_profiles()
    bundles = api.pages("bundleIds", {"filter[identifier]": BUNDLE, "limit": 200})
    bundles = [item for item in bundles if item["attributes"].get("identifier") == BUNDLE
               and item["attributes"].get("platform") in {"IOS", "UNIVERSAL"}
               and item["attributes"].get("seedId") == team]
    require(len(bundles) == 1, "exactly one registered iPhone bundle for the configured team is required")
    bundle_id = resource_id(bundles[0]["id"])
    certificates = api.pages("certificates", {"filter[certificateType]": "DISTRIBUTION,IOS_DISTRIBUTION", "limit": 200})
    matches = [item for item in certificates
               if item["attributes"].get("certificateType") in {"DISTRIBUTION", "IOS_DISTRIBUTION"}
               and base64.b64decode(item["attributes"]["certificateContent"], validate=True) == certificate]
    require(len(matches) == 1, "the imported distribution certificate must match exactly one existing Apple certificate")
    certificate_id = resource_id(matches[0]["id"])
    capabilities = api.pages(f"bundleIds/{bundle_id}/bundleIdCapabilities", {"limit": 200})
    profiles = api.pages(f"bundleIds/{bundle_id}/profiles", {"limit": 200})
    reusable = []
    for item in profiles:
        attrs = item["attributes"]
        if attrs.get("profileType") != "IOS_APP_STORE" or attrs.get("profileState") != "ACTIVE":
            continue
        payload = base64.b64decode(attrs["profileContent"], validate=True)
        profile = decode_profile(payload, directory)
        if profile.get("DeveloperCertificates") != [certificate]:
            continue
        if profile["ExpirationDate"].replace(tzinfo=UTC) <= datetime.now(UTC):
            continue
        if bound_profile(profile, attrs["name"], team, certificate):
            require(profile.get("UUID") == attrs["uuid"], "Apple profile UUID disagrees with its CMS content")
            reusable.append((profile["ExpirationDate"], resource_id(item["id"]), attrs["name"], payload))
    if reusable:
        _, profile_id, name, payload = max(reusable)
        return name, payload, {"action": "reused", "profile_id": profile_id, "push_capability_enabled": False}
    push = [item for item in capabilities if item["attributes"].get("capabilityType") == "PUSH_NOTIFICATIONS"]
    require(len(push) <= 1, "ambiguous push capability registration")
    enabled = False
    if not push:
        created = api.create("bundleIdCapabilities", {
            "type": "bundleIdCapabilities", "attributes": {"capabilityType": "PUSH_NOTIFICATIONS"},
            "relationships": {"bundleId": {"data": {"type": "bundleIds", "id": bundle_id}}},
        })
        created_id = resource_id(created["id"])
        refreshed = api.pages(f"bundleIds/{bundle_id}/bundleIdCapabilities", {"limit": 200})
        require(any(item["id"] == created_id and item["attributes"].get("capabilityType") == "PUSH_NOTIFICATIONS"
                    for item in refreshed), "Apple did not confirm push on the exact iPhone bundle")
        enabled = True
        print(json.dumps({"push_capability_enabled": True, "exact_iphone_bundle": True}), flush=True)
    context = os.environ["GITHUB_RUN_ID"] + "-" + os.environ["GITHUB_RUN_ATTEMPT"]
    require(re.fullmatch(r"[1-9][0-9]*-[1-9][0-9]*", context), "invalid protected job identity")
    name = f"AstralDeep iOS APNs {hashlib.sha256(certificate).hexdigest()[:16]} {context}"
    created = api.create("profiles", {
        "type": "profiles", "attributes": {"name": name, "profileType": "IOS_APP_STORE"},
        "relationships": {"bundleId": {"data": {"type": "bundleIds", "id": bundle_id}},
                          "certificates": {"data": [{"type": "certificates", "id": certificate_id}]}},
    })
    attrs = created["attributes"]
    require(attrs.get("profileType") == "IOS_APP_STORE" and attrs.get("profileState") == "ACTIVE"
            and attrs.get("name") == name, "Apple returned a mismatched or inactive iPhone profile")
    payload = base64.b64decode(attrs["profileContent"], validate=True)
    profile = decode_profile(payload, directory)
    require(bound_profile(profile, name, team, certificate), "Apple profile lacks production aps-environment")
    require(profile.get("UUID") == attrs["uuid"], "Apple profile UUID disagrees with its CMS content")
    return name, payload, {"action": "created", "profile_id": resource_id(created["id"]),
                           "push_capability_enabled": enabled}


def prepare(keychain, directory, key_path, receipt_path, name_path):
    team = os.environ["APPLE_TEAM_ID"]
    certificate = signing_certificate(keychain, team)
    phone = []
    watch = []
    for path in sorted(directory.glob("astral-ios-*.mobileprovision")):
        require(path.is_file() and not path.is_symlink(), "installed provisioning profile must be a regular file")
        payload = path.read_bytes()
        profile = decode_profile(payload, directory)
        app_id = profile.get("Entitlements", {}).get("application-identifier")
        if app_id == f"{team}.{BUNDLE}":
            phone.append((path, payload, profile))
        elif app_id == f"{team}.{BUNDLE}.watch":
            profile_identity(profile, BUNDLE + ".watch", os.environ["APPLE_PROFILE_WATCH"], team)
            watch.append((path, hashlib.sha256(payload).hexdigest()))
    require(len(phone) == len(watch) == 1, "exactly one installed iPhone and Watch profile is required")
    path, payload, profile = phone[0]
    name = os.environ["APPLE_PROFILE_IOS"]
    has_push = bound_profile(profile, name, team, certificate)
    receipt = {"action": "existing", "push_capability_enabled": False}
    if not has_push:
        name, payload, receipt = renewed_profile(AppleAPI(key_path), certificate, team, directory)
    descriptor, replacement = tempfile.mkstemp(prefix="astral-profile-prepared-", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
        os.replace(replacement, path)
    finally:
        Path(replacement).unlink(missing_ok=True)
    require(hashlib.sha256(watch[0][0].read_bytes()).hexdigest() == watch[0][1], "Watch profile changed unexpectedly")
    name_path.write_text(name + "\n")
    receipt.update(production_apns=True, exact_distribution_certificate=True,
                   certificate_sha256=hashlib.sha256(certificate).hexdigest(),
                   profile_sha256=hashlib.sha256(payload).hexdigest(), watch_profile_sha256=watch[0][1])
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ("keychain", "profiles-dir", "key", "receipt", "name-file"):
        parser.add_argument("--" + argument, type=Path, required=True)
    args = parser.parse_args()
    try:
        prepare(args.keychain, args.profiles_dir, args.key, args.receipt, args.name_file)
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        print(f"iPhone provisioning refused: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
