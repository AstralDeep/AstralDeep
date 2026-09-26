"""Capture native settings geometry from public catalog and shared view-builder source.
The offscreen artifacts are diagnostic and carry no authenticated acceptance claim.
"""

from pathlib import Path

source = Path(__file__).with_name("visual_iteration.py").read_text(encoding="utf-8")
exec(compile(source.split("out = ROOT /", 1)[0], str(Path(__file__)), "exec"))

sys.path.insert(0, str(PROJECTION / "src"))
from astralprojection.chrome.agents import build_agents_view
from PySide6.QtGui import QPainter

out = ROOT / "build/windows-091/surface-iteration"
out.mkdir(exist_ok=True)
application = QApplication([])
appmod.configure(application)
settings = QSettings(str(out / "diagnostic.ini"), QSettings.Format.IniFormat)
agents = [{**row, "owned": True, "is_public": True, "disabled": False} for row in catalog["agents"]]
components = [component.to_dict() for component in build_agents_view(agents).components]
with patch.object(appmod, "OrchestratorClient", _FakeClient), patch.object(appmod, "QSettings", lambda *a, **k: settings), patch.object(appmod.MainWindow, "_start_integrity_check", lambda self: None), patch.object(appmod.MainWindow, "_init_workspace", lambda self: None), patch.object(appmod, "load_or_create_voice_device_id", lambda: DEVICE):
    window = appmod.MainWindow("ws://127.0.0.1:9/ws", "", connect=False)
    window.client.connection_generation = CONNECTION
    window._continuity.bind_connection(CONNECTION)
    window._on_message({"type": "chrome_menu", "model": menu})
    window.show()
    records = []
    for width in (1440, 390):
        case = next(value for value in GEOMETRY if value["viewport"][0] == width)
        window.resize(*case["viewport"])
        window._on_message({"type": "rote_config", "device_profile": {"console": case["presentation"]}})
        window._console_open_surface("agents", "Agents & permissions", {})
        window._surface_dialog.set_surface("Agents & permissions", components)
        for _ in range(5):
            QApplication.processEvents()
            QTest.qWait(10)
        dialog = window._surface_dialog
        capture = window.grab()
        painter = QPainter(capture)
        offset = window.mapFromGlobal(dialog.pos())
        painter.drawPixmap(offset, dialog.grab())
        painter.end()
        filename = f"settings-agents-{width}x{window.height()}.png"
        capture.save(str(out / filename))
        records.append({"file": filename, "client": [window.width(), window.height()], "dialog": [offset.x(), offset.y(), dialog.width(), dialog.height()], "diagnostic_only": True})
        dialog.close()
    (out / "geometry.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    window.close()
