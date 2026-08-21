"""Native chat composers must not overlay a custom keyboard-dismiss control."""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
PROJECTION_ROOT = ROOT / "components" / "AstralProjection"

if not (
    (PROJECTION_ROOT / "apple-clients").is_dir()
    and (PROJECTION_ROOT / "android-client").is_dir()
):  # repo root absent inside the product image
    pytest.skip(
        "repo-root tooling files are not part of the product image",
        allow_module_level=True,
    )


def test_apple_chat_uses_native_immediate_scroll_keyboard_dismissal() -> None:
    source = (
        ROOT / "components/AstralProjection/apple-clients/AstralApp/AstralApp/Views/ChatView.swift"
    ).read_text(encoding="utf-8")

    assert "placement: .keyboard" not in source
    assert 'Button("Done")' not in source
    assert source.count(".scrollDismissesKeyboard(.immediately)") >= 2


def test_android_chat_leaves_dismissal_to_the_native_ime() -> None:
    source = (
        ROOT
        / "components/AstralProjection/android-client/app/src/main/kotlin/com/personalailabs/astraldeep/app/ui/AdaptiveShell.kt"
    ).read_text(encoding="utf-8")

    assert "keyboardOptions = KeyboardOptions(imeAction = ImeAction.Default)" in source
    assert "LocalSoftwareKeyboardController" not in source
    assert "keyboardController.hide()" not in source
