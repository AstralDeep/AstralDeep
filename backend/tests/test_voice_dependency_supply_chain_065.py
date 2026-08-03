"""Supply-chain contracts for feature 065 web and Android LiveKit clients."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
if not (REPO_ROOT / "android-client").is_dir():
    pytest.skip(
        "repo-root client manifests are not part of the product image",
        allow_module_level=True,
    )
WEB_VENDOR = REPO_ROOT / "backend" / "webrender" / "static" / "vendor"
ANDROID = REPO_ROOT / "android-client"

LIVEKIT_WEB_SHA256 = "a77a2f4c363e93099d7c135721c9ec81d6c5bacc691796dad799222e33cbfb31"
LIVEKIT_WEB_NOTICES_SHA256 = (
    "53c4b66c4a3a2c2c595fdce7c8d4a4d6389bec04843e80bce2ab6b21797e823e"
)
LIVEKIT_ANDROID_SHA256 = (
    "d3a85158392a0bf0ed0d835d4d5932ef3f166bbad2c80bb9a9b6bd08c42ac0a7"
)
WEBRTC_ANDROID_SHA256 = (
    "d2542864ce012f188d0b2d5da21f5cc48bacc6d46523d25f7515809d424780c6"
)
AUDIOSWITCH_SHA256 = "c8240221daa9a96d4ea01a4dc6f6f6b10b4903d2a71f9b57f838bdfeb6c3fcbc"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_livekit_web_umd_is_the_exact_approved_upstream_artifact() -> None:
    bundle = WEB_VENDOR / "livekit-client.umd.min.js"
    digest = WEB_VENDOR / "livekit-client.sha256"

    assert bundle.stat().st_size == 561_757
    assert _sha256(bundle) == LIVEKIT_WEB_SHA256
    assert digest.read_text(encoding="utf-8") == (
        f"{LIVEKIT_WEB_SHA256}  livekit-client.umd.min.js\n"
    )
    source = bundle.read_text(encoding="utf-8")
    assert source.startswith("!function(")
    assert "LivekitClient" in source


def test_livekit_web_upstream_license_is_bundled_verbatim() -> None:
    license_file = WEB_VENDOR / "LICENSE.livekit-client"

    assert license_file.stat().st_size == 10_142
    assert _sha256(license_file) == (
        "09e8a9bcec8067104652c168685ab0931e7868f9c8284b66f5ae6edae5f1130b"
    )
    text = license_file.read_text(encoding="utf-8")
    assert "Apache License" in text
    assert "Version 2.0, January 2004" in text


def test_livekit_web_third_party_notices_are_complete_and_checksum_pinned() -> None:
    notices = WEB_VENDOR / "THIRD_PARTY_NOTICES.livekit-client"
    checksum = WEB_VENDOR / "THIRD_PARTY_NOTICES.livekit-client.sha256"

    assert notices.stat().st_size == 16_043
    assert _sha256(notices) == LIVEKIT_WEB_NOTICES_SHA256
    assert checksum.read_text(encoding="utf-8") == (
        f"{LIVEKIT_WEB_NOTICES_SHA256}  THIRD_PARTY_NOTICES.livekit-client\n"
    )

    text = notices.read_text(encoding="utf-8")
    for provenance in (
        "v2.21.0 (peeled commit 15ca5f8180ab8939c3a5a4dfee1d5e44f62f71cf)",
        "fb80a81a094f96f764d43a6e366f04ab22d78aa8d562252eef790f93ca8274e4",
        "205a3d49070d350702dc44b6c20045c0cc2aae22f19117efa8ebea7375caf097",
        "51b0d7c79176d3b4accba57755f13b63df404013f5e913883b3507fdd5ea93f5",
        LIVEKIT_WEB_SHA256,
    ):
        assert provenance in text

    for package_line in (
        "@livekit/mutex                       1.1.1     Apache-2.0",
        "@livekit/protocol                    1.50.4    Apache-2.0",
        "@bufbuild/protobuf                   1.10.1    Apache-2.0 AND BSD-3-Clause",
        "events                               3.3.0     MIT",
        "jose                                 6.2.3     MIT",
        "loglevel                             1.9.2     MIT",
        "sdp-transform                        2.15.0    MIT",
        "webrtc-adapter                       9.0.6     BSD-3-Clause",
        "sdp (webrtc-adapter transitive)      3.2.2     MIT",
        "tslib                                2.8.1     0BSD",
        "typed-emitter                        2.1.0     MIT",
    ):
        assert package_line in text

    assert "`@types/dom-mediacapture-record`" in text
    assert "are not part of the runtime closure" in text


def test_livekit_web_notices_retain_component_attributions_and_license_terms() -> None:
    text = (WEB_VENDOR / "THIRD_PARTY_NOTICES.livekit-client").read_text(
        encoding="utf-8"
    )

    for attribution in (
        "Copyright 2008 Google Inc.  All rights reserved.",
        "Copyright (c) 2014, The WebRTC project authors. All rights reserved.",
        "Copyright Joyent, Inc. and other Node contributors.",
        "Copyright (c) 2018 Filip Skokan",
        "Copyright (c) 2013 Tim Perry",
        "Copyright (c) 2013 Eirik Albrigtsen",
        "Copyright (c) 2017 Philipp Hancke",
        "Copyright (c) 2018 Andy Wermke",
        "Copyright (c) Microsoft Corporation.",
        "Copyright (c) 2017 Jakub Chodorowicz",
    ):
        assert attribution in text

    assert "Redistribution and use in source and binary forms" in text
    assert "Permission to use, copy, modify, and/or distribute this software" in text
    assert "Permission is hereby granted, free of charge" in text
    assert "LICENSE.livekit-client" in text


def test_android_catalog_and_app_pin_approved_livekit_and_protobuf() -> None:
    catalog = (ANDROID / "gradle" / "libs.versions.toml").read_text(encoding="utf-8")
    app = (ANDROID / "app" / "build.gradle.kts").read_text(encoding="utf-8")

    assert re.search(r'^livekitAndroid\s*=\s*"2[.]27[.]0"$', catalog, re.MULTILINE)
    assert 'strictly = "3.25.5"' in catalog
    assert 'module = "io.livekit:livekit-android"' in catalog
    assert 'module = "com.google.protobuf:protobuf-javalite"' in catalog
    assert "implementation(libs.livekit.android)" in app
    assert "implementation(libs.protobuf.javalite)" in app


def test_android_jitpack_repository_is_module_filtered_to_audioswitch() -> None:
    settings = (ANDROID / "settings.gradle.kts").read_text(encoding="utf-8")
    jitpack = re.search(
        r'maven\s*\{(?P<body>.*?url\s*=\s*uri\("https://jitpack[.]io"\).*?)\n\s*\}',
        settings,
        re.DOTALL,
    )
    assert jitpack, "JitPack repository is missing"
    body = jitpack.group("body")
    assert 'includeModule("com.github.davidliu", "audioswitch")' in body
    assert "includeGroup" not in body
    assert settings.count("https://jitpack.io") == 1


def test_android_lock_replaces_vulnerable_protobuf_and_pins_native_closure() -> None:
    lock = (ANDROID / "app" / "gradle.lockfile").read_text(encoding="utf-8")

    for coordinate in (
        "io.livekit:livekit-android:2.27.0=",
        "io.github.webrtc-sdk:android-prefixed:144.7559.09=",
        "com.github.davidliu:audioswitch:039a35aefab7747c557242fa216c9ea11743b604=",
        "com.google.protobuf:protobuf-javalite:3.25.5=",
    ):
        assert coordinate in lock
    assert "com.google.protobuf:protobuf-javalite:3.22.0=" not in lock


def test_android_verification_metadata_hashes_approved_binary_artifacts() -> None:
    metadata = (ANDROID / "gradle" / "verification-metadata.xml").read_text(
        encoding="utf-8"
    )
    assert "<verify-metadata>true</verify-metadata>" in metadata
    assert 'name="protobuf-javalite" version="3.22.0"' not in metadata

    for component, artifact, digest in (
        (
            'group="io.livekit" name="livekit-android" version="2.27.0"',
            'name="livekit-android-2.27.0.aar"',
            LIVEKIT_ANDROID_SHA256,
        ),
        (
            'group="io.github.webrtc-sdk" name="android-prefixed" version="144.7559.09"',
            'name="android-prefixed-144.7559.09.aar"',
            WEBRTC_ANDROID_SHA256,
        ),
        (
            'group="com.github.davidliu" name="audioswitch" '
            'version="039a35aefab7747c557242fa216c9ea11743b604"',
            'name="audioswitch-039a35aefab7747c557242fa216c9ea11743b604.aar"',
            AUDIOSWITCH_SHA256,
        ),
    ):
        assert component in metadata
        assert artifact in metadata
        assert f'value="{digest}"' in metadata

    aapt2 = metadata.split(
        '<component group="com.android.tools.build" name="aapt2" '
        'version="9.2.1-15009934">',
        1,
    )[1].split("</component>", 1)[0]
    assert 'name="aapt2-9.2.1-15009934-linux.jar"' in aapt2
    linux_aapt2 = aapt2.split(
        '<artifact name="aapt2-9.2.1-15009934-linux.jar">',
        1,
    )[1].split("</artifact>", 1)[0]
    assert (
        'value="755f6727fb3f4cce5e319eac0f3618ed4b36b49a46d4bb2cbb6fa8e9175a54d6"'
        in linux_aapt2
    )
