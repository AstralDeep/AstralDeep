"""Capture public responsive fixtures through isolated offscreen native transport.
These images are reproducible visual diagnostics, never authenticated acceptance evidence.
"""

import argparse
import asyncio
import copy
import hashlib
import importlib.util
import json
import os
import platform
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[3]


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    embedded = ROOT / "components/AstralProjection"
    sibling = ROOT.parent / "AstralProjection"
    projection = Path(
        os.environ.get(
            "ASTRAL_PROJECTION_ROOT", embedded if embedded.is_dir() else sibling
        )
    )
    suffix = Path("assets/astral-native-ui-v2/web-reference")
    references = [
        ROOT.parent.parent / "kos-wiki" / suffix,
        ROOT.parent / "kos-wiki" / suffix,
    ]
    parser.add_argument(
        "--projection-root",
        type=Path,
        default=projection,
        help="Projection checkout; defaults to the embedded component, then repository sibling.",
    )
    parser.add_argument(
        "--reference-root",
        type=Path,
        default=next((path for path in references if path.is_dir()), references[0]),
        help="The kos-wiki web-reference screenshot directory.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "build/windows-091/diagnostic-matrix",
        help="Destination for diagnostic images and provenance manifest.",
    )
    args = parser.parse_args(argv)
    for key in ("projection_root", "reference_root", "output"):
        setattr(args, key, getattr(args, key).expanduser().resolve())
    if not (args.projection_root / "windows-client/astral_client/app.py").is_file():
        parser.error(
            "--projection-root must contain the Windows client source and test fixtures"
        )
    if not (args.reference_root / "manifest.json").is_file():
        parser.error(
            "--reference-root must contain the archived web references and manifest.json"
        )
    return args


def main(argv=None):
    args = arguments(argv)
    projection = args.projection_root.expanduser().resolve()
    os.environ["ASTRAL_PROJECTION_ROOT"] = str(projection)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["ASTRAL_WIN_AGENT"] = "0"
    os.environ["ASTRAL_WINDOWS_PROFILE_ID"] = str(uuid.uuid4())
    from visual_iteration import (
        CONNECTION,
        DEVICE,
        GEOMETRY,
        QApplication,
        QSettings,
        QTest,
        _FakeClient,
        appmod,
        catalog,
        composer,
        frame,
        literal_assignments,
        menu as template_menu,
    )
    import PySide6
    from PySide6.QtCore import QEvent, QPoint
    from PySide6.QtGui import QPainter
    from astralprojection.chrome.agents import build_agents_view
    from astralprojection.chrome.guidance import build_selection_form
    from webrender.chrome.menu_model import menu_model_dict
    from test_console_shell import CHAT
    from test_conversation_continuity_060 import _snapshot

    if not Path(appmod.__file__).resolve().is_relative_to(projection):
        raise RuntimeError("The imported native client does not belong to --projection-root")
    menu = copy.deepcopy(template_menu)
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    reference = args.reference_root.resolve()
    application = QApplication([])
    appmod.configure(application)
    settings = QSettings(
        str(out / "isolated-diagnostic.ini"), QSettings.Format.IniFormat
    )
    settings.clear()
    shared_menu = menu_model_dict(
        ["admin"],
        pulse_enabled=True,
        byo_enabled=True,
        remote_enabled=True,
        notes_enabled=True,
        export_enabled=True,
        share_enabled=True,
    )
    menu.update(shared_menu)
    menu["console"]["composer_actions"] = [
        item for item in menu["console"]["composer_actions"] if item["key"] != "work"
    ]
    agents = [
        {**row, "owned": True, "is_public": True, "disabled": False}
        for row in catalog["agents"]
    ]
    settings_components = [
        item.to_dict() for item in build_agents_view(agents).components
    ]
    selection_components = [
        item.to_dict()
        for item in build_selection_form(
            dict(
                status="ready",
                agents=[],
                skills=[],
                notes=[],
                selected=dict(agent=None, skills=[], notes=[]),
            )
        ).components
    ]
    intro_spec = importlib.util.spec_from_file_location(
        "diagnostic_agent_intro",
        ROOT / "backend/orchestrator/projection_surfaces/agent_intro.py",
    )
    intro_builder = importlib.util.module_from_spec(intro_spec)
    intro_spec.loader.exec_module(intro_builder)
    dice = literal_assignments(
        ROOT / "backend/agents/dice_roller/dice_roller_agent.py",
        ["agent_id", "service_name", "description", "examples"],
    )
    card = SimpleNamespace(
        name=dice["service_name"],
        description=dice["description"],
        metadata=dict(examples=dice["examples"]),
        skills=[SimpleNamespace(id="roll_dice", name="roll_dice")],
    )
    with patch.object(
        intro_builder, "_visible_agent", AsyncMock(return_value=(card, True))
    ):
        intro_components = asyncio.run(
            intro_builder.components(
                None,
                "diagnostic-only",
                [],
                {"agent_id": dice["agent_id"]},
                console_contract="console/v2",
            )
        )

    source_files = [
        "app.py",
        "console_widgets.py",
        "surface_widgets.py",
        "renderer.py",
        "theme.py",
        "typography.py",
        "icons.py",
        "viewport.py",
        "surface_backdrop.py",
        "composites.py",
    ]

    def hashes():
        return {
            name: hashlib.sha256(
                (projection / "windows-client/astral_client" / name).read_bytes()
            ).hexdigest()
            for name in source_files
        }

    def settle():
        for _ in range(8):
            QApplication.processEvents()
            QTest.qWait(10)

    def bounds(widget, root):
        pos = widget.mapTo(root, widget.rect().topLeft())
        return [pos.x(), pos.y(), widget.width(), widget.height()]

    records = []
    start_hashes = hashes()
    started = datetime.now(timezone.utc).isoformat()
    with (
        patch.object(appmod, "OrchestratorClient", _FakeClient),
        patch.object(appmod, "create_settings", lambda *a, **k: settings),
        patch.object(appmod.MainWindow, "_start_integrity_check", lambda self: None),
        patch.object(appmod.MainWindow, "_init_workspace", lambda self: None),
        patch.object(appmod, "load_or_create_voice_device_id", lambda: DEVICE),
    ):
        for width, height in [
            (1440, 900),
            (1280, 800),
            (1024, 768),
            (834, 1194),
            (768, 1024),
            (390, 844),
            (320, 740),
        ]:
            window = appmod.MainWindow("ws://127.0.0.1:9/ws", "", connect=False)
            window.client.connection_generation = CONNECTION
            window.client.authenticated = True
            window._resume_store.storage_key = "offscreen-diagnostic-owner"
            window._continuity.bind_connection(CONNECTION)
            window._on_message({"type": "chrome_menu", "model": menu})
            geometry = next(
                item for item in GEOMETRY if item["viewport"] == [width, height]
            )
            window.resize(width, height)
            window._on_message(
                {
                    "type": "rote_config",
                    "device_profile": {"console": geometry["presentation"]},
                }
            )
            window.show()
            window._voice_widget.apply_composer_state(frame(composer), CONNECTION)
            settle()
            window._viewport_timer.stop()
            window._viewport.deferred.stop()
            shell = window._console_shell
            settle()

            def capture(state, overlay=None):
                focused = QApplication.focusWidget()
                if focused is not None:
                    focused.clearFocus()
                window._input.clearFocus()
                shell.results.fullscreen_button.clearFocus()
                QApplication.processEvents()
                filename = f"{state}-{width}x{height}.png"
                pixmap = window.grab()
                extras = {}
                if overlay is not None:
                    painter = QPainter(pixmap)
                    offset = window.mapFromGlobal(overlay.pos())
                    painter.drawPixmap(offset, overlay.grab())
                    painter.end()
                    extras["overlay"] = [
                        offset.x(),
                        offset.y(),
                        overlay.width(),
                        overlay.height(),
                    ]
                if not pixmap.save(str(out / filename)):
                    raise OSError(f"Could not save {out / filename}")
                reference_file = reference / filename
                records.append(
                    dict(
                        file=filename,
                        viewport=[width, height],
                        actual=[window.width(), window.height()],
                        diagnostic_only=True,
                        device_pixel_ratio=pixmap.devicePixelRatio(),
                        sha256=hashlib.sha256(
                            (out / filename).read_bytes()
                        ).hexdigest(),
                        reference=str(reference_file),
                        reference_sha256=hashlib.sha256(
                            reference_file.read_bytes()
                        ).hexdigest(),
                        widgets={
                            name: bounds(getattr(shell, name), window)
                            for name in [
                                "sidebar",
                                "header",
                                "title",
                                "subtitle",
                                "categories_scroll",
                                "scenarios",
                                "composer",
                            ]
                        },
                        **extras,
                    )
                )

            capture("landing")
            if geometry["presentation"]["navigation_mode"] == "drawer":
                shell.toggle_drawer()
                settle()
                shell.history_button.clearFocus()
                capture("navigation-drawer")
                shell.close_drawer()
                settle()
            for state, surface, title, params, components in [
                (
                    "agent-intro",
                    "agent_intro",
                    "Dice Roller",
                    {"agent_id": dice["agent_id"]},
                    intro_components,
                ),
                (
                    "chat-selection",
                    "guidance",
                    "Use for this chat",
                    {"view": "selection"},
                    selection_components,
                ),
                (
                    "settings-agents",
                    "agents",
                    "Agents & permissions",
                    {},
                    settings_components,
                ),
            ]:
                window._console_open_surface(surface, title, params)
                dialog = window._surface_dialog
                dialog.set_surface(title, components)
                settle()
                dialog._scroll.verticalScrollBar().setValue(0)
                capture(state, dialog)
                dialog.close()
                settle()
            menu_widget = shell.more_menu
            shell._show_more()
            settle()
            actual_popup = menu_widget.pos()
            compact = (
                geometry["presentation"]["settings_navigation_axis"] == "horizontal"
            )
            anchor_widget = shell.composer if compact else shell.more_button
            right = anchor_widget.width() - (
                shell.composer.layout().contentsMargins().right() if compact else 0
            )
            top = shell.more_button.mapTo(anchor_widget, QPoint(0, 0)).y()
            intended_popup = anchor_widget.mapToGlobal(
                QPoint(
                    right - menu_widget.width(),
                    top - menu_widget.height() - (8 if compact else 10),
                )
            )
            menu_widget.move(intended_popup)
            menu_widget.setActiveAction(None)
            capture("composer-more", menu_widget)
            records[-1]["popup_before_offscreen_screen_clamp_correction"] = [
                actual_popup.x(),
                actual_popup.y(),
            ]
            records[-1]["popup_screen_clamp_corrected"] = actual_popup != intended_popup
            menu_widget.hide()
            settle()
            window._set_active_chat(CHAT, persist=False)
            generation = window._begin_conversation_request("hydration", CHAT)
            snapshot = _snapshot(
                request=generation, snapshot_id=str(uuid.uuid4()), revision=7
            )
            prompt = dice["examples"][0]["prompt"]
            snapshot["transcript"] = [
                dict(
                    message_id="1841",
                    role="user",
                    created_at="2026-09-26T14:41:00Z",
                    attachments=[],
                    parts=[dict(type="text", text=prompt)],
                ),
                dict(
                    message_id="1842",
                    role="assistant",
                    created_at="2026-09-26T14:41:01Z",
                    attachments=[],
                    parts=[
                        dict(
                            type="text",
                            text="Rolled 6d6: **5, 3, 3, 2, 2, 1** — total **16** pips (normalized range 6–36, so 16 is just under the median).",
                        )
                    ],
                ),
            ]
            snapshot["canvas"]["components"] = [
                dict(
                    type="card",
                    component_id="dice-result",
                    source_agent="dice_roller",
                    title="Dice Roll Results (6d6)",
                    content=[
                        dict(
                            type="grid",
                            columns=1 if width < 768 else 2,
                            gap=20,
                            children=[
                                dict(
                                    type="metric",
                                    title="Total",
                                    value="16",
                                    variant="default",
                                ),
                                dict(
                                    type="metric",
                                    title="Number of dice",
                                    value="6",
                                    variant="default",
                                ),
                            ],
                        ),
                        dict(type="divider", variant="solid"),
                        dict(
                            type="text",
                            content="Individual rolls (1–6 each):",
                            variant="subheading",
                        ),
                        dict(
                            type="list",
                            ordered=False,
                            variant="default",
                            items=[
                                f"Die {index + 1}: {value}"
                                for index, value in enumerate([5, 3, 3, 2, 2, 1])
                            ],
                        ),
                    ],
                )
            ]
            window._on_message(snapshot)
            window._input.setText(prompt)
            shell.results.set_metadata("Dice Roller", 1)
            shell.scroll.verticalScrollBar().setValue(0)
            settle()
            capture("result-success")
            shell.set_fullscreen(True)
            settle()
            capture("result-fullscreen")
            shell.set_fullscreen(False)
            settle()
            window.close()
            window.deleteLater()
            QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            settle()
            print(f"Captured {width}x{height}", flush=True)
    report = dict(
        evidence="Synthetic offscreen diagnostic, fake transport, fixture identity/catalog, no network connection. NOT authenticated UI, dispatch, voice-worker, or native DPI acceptance.",
        runtime={
            "python": platform.python_version(),
            "pyside6": PySide6.__version__,
            "qt_platform": application.platformName(),
            "offscreen_screen": [
                application.primaryScreen().size().width(),
                application.primaryScreen().size().height(),
            ],
        },
        started_at=started,
        completed_at=datetime.now(timezone.utc).isoformat(),
        generator={
            "path": str(Path(__file__).relative_to(ROOT)),
            "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "projection_root": str(projection),
            "reference_root": str(reference),
            "imported_client": str(Path(appmod.__file__).resolve()),
        },
        heads={
            name: subprocess.check_output(
                ["git", "-C", str(path), "rev-parse", "HEAD"], text=True
            ).strip()
            for name, path in [("deep", ROOT), ("projection", projection)]
        },
        source_sha256_before=start_hashes,
        source_sha256_after=hashes(),
        catalog_source="Public literal assignments in Deep backend/agents and welcome.py/web_landing.py",
        payload_sources=[
            "Projection chrome agents/guidance shared builders",
            "Deep agent_intro.components with isolated synthetic visibility fixture",
            "Existing Windows continuity snapshot fixture with public reference dice result",
        ],
        capture_notes=[
            "More popup opened through ConsoleShell._show_more. Only the diagnostic helper restores the same intended client-relative anchor when the 800x800 offscreen screen clamps the separate popup window; pre-correction global coordinates are recorded.",
            "QDialog and QMenu separate Qt windows composited at native relative positions into client grab.",
            "Catalog currently includes 11 public source agents; reference authenticated account showed 10.",
            "No credentials, real account history, or authenticated user data used.",
        ],
        captures=records,
    )
    (out / "manifest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out / "README.md").write_text(
        "# Windows diagnostic matrix\n\n53 synthetic offscreen captures: 7 reference states at 7 sizes plus the 4 drawer sizes. These are visual implementation diagnostics, **not authenticated acceptance evidence**. Public shared builders and fake transport are used; no network connection, credential entry, permission bypass, or Computer Use occurs.\n\nSee manifest.json for current Git heads, dirty source content hashes, each image and reference digest, capture timestamps and limitations. The preserved web provider error is excluded because it is an actual historical failure, not one of these eight requested states.\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "captures": len(records),
                "source_stable": start_hashes == hashes(),
                "out": str(out),
            }
        )
    )


if __name__ == "__main__":
    main()
