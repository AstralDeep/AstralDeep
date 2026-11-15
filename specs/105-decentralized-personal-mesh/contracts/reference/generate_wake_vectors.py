"""Builds deterministic shared wake-route validation and current-feed fold cases for the vector generator.
Inputs are synthetic signed-header projections; separate event vectors cover actual signatures and header admission."""

import copy

from astral_mesh_ref import crypto, wake
from astral_mesh_ref.testkit import TestDevice


def build_wake(suite):
    result = suite("wake", "contracts/wake.md")
    alice, bob, retired = (TestDevice(name, "desktop") for name in ("wake-a", "wake-b", "wake-retired"))
    labels = {"confidentiality": "private", "integrity": "owner"}

    def route(capability="opaque_capability", expires=2000):
        return {"publisher": "astral", "capability": capability, "expires_at_ms": expires}

    def check(name, body, signed_ms=1000, *, valid=True):
        result.add(name, "wake_route_validate", {"body": body, "signed_ms": signed_ms},
                   **({"expect": {"route": body["route"]}} if valid else {"error": "wake.malformed"}))

    check("valid/tombstone", {"route": None})
    for suffix in ("", "=", "=="):
        check(f"valid/padding-{len(suffix)}", {"route": route("opaque_token" + suffix)})
    check("valid/maximum-capability", {"route": route("a" * wake.MAX_CAPABILITY_BYTES)})
    check("valid/maximum-ttl", {"route": route(expires=1000 + wake.MAX_TTL_MS)})
    check("valid/maximum-safe-expiry", {"route": route(expires=wake.MAX_SAFE_EXPIRY)}, wake.MAX_SAFE_EXPIRY - 1)
    malformed = [
        ("body-not-object", []), ("body-missing-route", {}), ("body-extra-field", {"route": None, "device": "other"}),
        ("route-not-object", {"route": "value"}), ("missing-publisher", {"route": {"capability": "x", "expires_at_ms": 2000}}),
        ("extra-target", {"route": {**route(), "device": alice.id}}),
        ("unknown-publisher", {"route": {**route(), "publisher": "other"}}),
    ]
    for name, body in malformed:
        check("invalid/" + name, body, valid=False)
    for index, capability in enumerate([None, True, "", "a.b", "https://example.test", "a b", "a\nb", "a+b", "a/b", "é", "=abc", "a=b", "abc===", "a" * 2049]):
        check(f"invalid/capability-{index}", {"route": route(capability)}, valid=False)
    for index, expiry in enumerate([None, True, "2000", -1, 0, 1000, 999, 1000 + wake.MAX_TTL_MS + 1, wake.MAX_SAFE_EXPIRY + 1]):
        check(f"invalid/expiry-{index}", {"route": route(expires=expiry)}, valid=False)

    def event(name, device=alice, ms=1000, *, body=None, state="present", kind="device.wake_route", declared=None):
        return {"id": crypto.sha256_id(name.encode()), "feed": device.feed, "seq": ms,
                "clock": {"ms": ms, "n": 0}, "type": kind, "labels": dict(labels if declared is None else declared),
                "body": {"route": route(name, 10000)} if body is None else body, "state": state}

    current = {alice.feed: alice.id, bob.feed: bob.id}
    old = event("old", ms=1000)
    latest = event("latest", ms=2000)
    peer = event("peer", device=bob, ms=1500)

    def folded(name, events, expected, *, now_ms=3000, feeds=None):
        value = {"events": events, "current_device_of_feed": current if feeds is None else feeds, "now_ms": now_ms}
        for reverse in [False, True]:
            case = copy.deepcopy(value)
            if reverse:
                case["events"].reverse()
            result.add(f"fold/{name}-{'reverse' if reverse else 'forward'}", "wake_route_fold", case,
                       expect={"routes": sorted(expected, key=lambda entry: entry["device_id"])})

    def expected(device, value):
        return {"device_id": device.id, "route": value["body"]["route"]}

    folded("latest-per-current-device", [old, latest, peer], [expected(alice, latest), expected(bob, peer)])
    folded("retired-and-foreign-feeds-ignored", [old, event("retired", retired, 9000)], [expected(alice, old)])
    folded("removed-device-ignored", [old, peer], [expected(alice, old)], feeds={alice.feed: alice.id})
    folded("tombstone-does-not-revive-older", [old, event("clear", ms=2000, body={"route": None})], [])
    folded("expired-newest-does-not-revive-older", [old, event("expired", ms=2000, body={"route": route("expired", 2500)})], [])
    folded("exact-expiry-is-unavailable", [latest], [], now_ms=10000)
    folded("unreadable-newest-does-not-revive-older", [old, event("unreadable", ms=2000, state="unavailable")], [])
    folded("malformed-newest-does-not-revive-older", [old, event("bad", ms=2000, body={"route": route("a.b")})], [])
    folded("unrelated-kind-ignored", [old, event("memory", ms=2000, kind="settings.set")], [expected(alice, old)])
    folded("untrusted-header-ignored", [old, event("untrusted", ms=2000, declared={"confidentiality": "private", "integrity": "untrusted"})], [expected(alice, old)])
    folded("public-header-ignored", [old, event("public", ms=2000, declared={"confidentiality": "public", "integrity": "owner"})], [expected(alice, old)])
    return result
