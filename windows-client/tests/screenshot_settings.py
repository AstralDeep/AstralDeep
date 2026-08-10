"""Settings-parity screenshot harness (the desktop half of the cross-client
settings verification; follows tests/screenshot.py — feature 044 T052 rules:
native platform, real fonts, loud font gate).

Drives the real MainWindow against a running (mock-auth) orchestrator, opens
the server-owned Settings dropdown and EVERY settings entry — the SDUI surfaces
(llm / personalization / theme / guide, via the same ``chrome_open`` →
``chrome_surface`` round-trip the Android client uses) and the native dialogs
(agents / audit) — and grabs each to a PNG.

Usage:  python tests/screenshot_settings.py --out build/verify
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# This harness drives the real MainWindow; it must not open the
# client-hosted tools listener on a network port for the capture's lifetime.
os.environ.setdefault("ASTRAL_WIN_AGENT", "0")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication  # noqa: E402

from astral_client.app import MainWindow, configure  # noqa: E402
from screenshot import assert_fonts_legible, grab, grab_settings_menu, pump  # noqa: E402

#: The SDUI settings surfaces (server components rendered natively).
SDUI_SURFACES = [
    ("llm", "LLM settings", "win_03_llm.png"),
    ("personalization", "Personalization", "win_04_personalization.png"),
    ("theme", "Theme", "win_05_theme.png"),
    ("guide", "User guide", "win_06_guide.png"),
]


def grab_opaque(widget, app, path, tries=4) -> bool:
    """Grab ``widget`` to ``path``, compositing over the brand background.

    ``QWidget.grab`` renders child painting but not the top-level window's
    backing-store fill, so a dialog capture carries a transparent background
    (on SCREEN the window is opaque — this is purely a capture artifact; a
    text-heavy surface like the guide previews as "washed out"). Flatten onto
    the palette background so the evidence matches what the user sees."""
    from PySide6.QtGui import QColor, QImage, QPainter

    from astral_client import theme as T

    _ = tries
    pump(app, 0.4)
    img = widget.grab().toImage()
    if img.hasAlphaChannel():
        base = QImage(img.size(), QImage.Format_RGB32)
        base.fill(QColor(getattr(T, "PALETTE", {}).get("bg", "#0F1221")))
        p = QPainter(base)
        p.drawImage(0, 0, img)
        p.end()
        return base.save(path)
    return img.save(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8001/ws")
    ap.add_argument("--token", default="dev-token")
    ap.add_argument("--out", default=".")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    app = QApplication(sys.argv)
    configure(app)
    family = assert_fonts_legible()  # T052: refuse to emit tofu evidence
    print("font gate OK — rendering with:", family.encode("ascii", "replace").decode())
    win = MainWindow(args.url, args.token)
    win.resize(1300, 860)
    win.show()
    pump(app, 5)  # connect + chrome_menu + welcome canvas

    grab(win, os.path.join(args.out, "win_01_welcome.png"))
    grab_settings_menu(win, app, os.path.join(args.out, "win_02_settings_menu.png"))

    for surface, label, fname in SDUI_SURFACES:
        win._open_surface(surface, label)
        pump(app, 3.0)
        dlg = win._surface_dialog
        grab_opaque(dlg, app, os.path.join(args.out, fname))
        print("saved", fname, "| title:",
              dlg.windowTitle().encode("ascii", "replace").decode())
        dlg.hide()

    # Native dialogs — the Windows twins of Android's Agents/Audit screens.
    win._open_surface("agents", "Agents & permissions")
    pump(app, 3.0)
    grab_opaque(win._agents_dialog, app, os.path.join(args.out, "win_07_agents.png"))
    print("saved win_07_agents.png")
    win._agents_dialog.hide()

    win._open_surface("audit", "Audit log")
    pump(app, 3.0)
    grab_opaque(win._audit_dialog, app, os.path.join(args.out, "win_08_audit.png"))
    print("saved win_08_audit.png")
    win._audit_dialog.hide()

    win.client.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
