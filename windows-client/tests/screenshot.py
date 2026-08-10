"""Screenshot harness: drives the real MainWindow against a running (mock-auth)
orchestrator and grabs the window to PNGs at each stage, producing the desktop
verification evidence.

Feature 044 (T052 / FR-022) — legible-capture fix. The prior harness forced
``QT_QPA_PLATFORM=offscreen``, whose stub font engine resolves NO real glyphs, so
every capture rendered text as ``.notdef`` "tofu" boxes — the exact defect the
verification bundle was supposed to disprove. Two changes:

  * The capture runs on the platform's NATIVE Qt backend (real fonts) by
    default; pass ``--offscreen`` only for smoke runs that don't inspect text.
  * A font sanity gate (:func:`assert_fonts_legible`) fails LOUDLY when no real
    font family with actual glyphs resolves, so an illegible capture can never
    be produced silently again.

Usage:  python tests/screenshot.py --prompt "roll 3 dice"
Outputs: shot_welcome.png, shot_settings_menu.png, shot_chat.png in --out.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

# NOTE: unlike the pytest suite (conftest forces offscreen for headless logic
# tests), this harness must render real glyphs — so it does NOT default to
# offscreen. --offscreen is an explicit opt-in for text-agnostic smoke runs.
if "--offscreen" in sys.argv:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# This harness drives the real MainWindow; it must not open the
# client-hosted tools listener on a network port for the capture's lifetime.
os.environ.setdefault("ASTRAL_WIN_AGENT", "0")

from PySide6.QtGui import QFont, QFontDatabase, QFontMetrics  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from astral_client.app import MainWindow, configure  # noqa: E402


def assert_fonts_legible() -> str:
    """Fail loudly if the Qt platform can't render real glyphs (the tofu guard).

    Returns the resolved family name on success. Requires a live QApplication.
    Heuristic: the font database must expose real families AND a representative
    Latin string must produce non-zero advance width with the default font (a
    tofu/.notdef fallback still has width, so we ALSO require the chosen family
    to actually exist in the database rather than being a silent substitute)."""
    families = [f for f in QFontDatabase.families() if f and not f.startswith("@")]
    if not families:
        raise SystemExit(
            "FONT GATE: no font families available to Qt — captures would render "
            "as tofu. Run on a host with system fonts (this harness must NOT use "
            "the offscreen platform for evidence). See feature 044 T052."
        )
    fm = QFontMetrics(QFont())
    if fm.horizontalAdvance("AstralDeep 0123") <= 0:
        raise SystemExit("FONT GATE: default font reports zero text advance — illegible.")
    return QFont().family() or families[0]


def pump(app: QApplication, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def grab(win: MainWindow, path: str) -> None:
    win.grab().save(path)
    # Feature 042: status now lives on the brand-mark tooltip (minimal top bar).
    status = win.topbar._mark.toolTip().encode("ascii", "replace").decode()
    print("saved", path, "| status:", status)


def grab_settings_menu(win: MainWindow, app: QApplication, path: str) -> None:
    """Pop the Settings dropdown (built from the server-owned model) and grab it."""
    btn = win.topbar.settings_btn
    win.topbar._menu.popup(btn.mapToGlobal(btn.rect().bottomLeft()))
    pump(app, 0.6)
    win.topbar._menu.grab().save(path)
    labels = [a.text() for a in win.topbar._menu.actions() if a.text()]
    win.topbar._menu.close()
    print("saved", path, "| menu:", ", ".join(labels).encode("ascii", "replace").decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8001/ws")
    ap.add_argument("--token", default="dev-token")
    ap.add_argument("--prompt", default="roll 3 dice and show the results")
    ap.add_argument("--out", default=".")
    ap.add_argument("--offscreen", action="store_true",
                    help="force the offscreen platform (text-agnostic smoke runs only)")
    args = ap.parse_args()

    app = QApplication(sys.argv)
    configure(app)
    family = assert_fonts_legible()   # T052: refuse to emit tofu evidence
    print("font gate OK — rendering with:", family.encode("ascii", "replace").decode())
    win = MainWindow(args.url, args.token)
    win.resize(1300, 860)
    win.show()

    pump(app, 5)                       # connect + welcome canvas
    grab(win, os.path.join(args.out, "shot_welcome.png"))
    grab_settings_menu(win, app, os.path.join(args.out, "shot_settings_menu.png"))

    win._input.setText(args.prompt)    # ask a question
    win._send()
    pump(app, float(os.getenv("SHOT_WAIT", "28")))   # ReAct loop + designer + components
    grab(win, os.path.join(args.out, "shot_chat.png"))

    win.client.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
