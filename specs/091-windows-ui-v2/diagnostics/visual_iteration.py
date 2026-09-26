"""Render diagnostic native geometry using public server catalog source and test transport.
These offscreen images are implementation diagnostics, not authenticated visual acceptance.
"""
import ast
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
PROJECTION = ROOT / 'components/AstralProjection'
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['ASTRAL_WIN_AGENT'] = '0'
sys.path[:0] = [str(PROJECTION / 'windows-client'), str(PROJECTION / 'windows-client/tests')]
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QPushButton
from PySide6.QtTest import QTest
from astral_client import app as appmod
from test_message_routing import _FakeClient
from test_console_shell import MENU, GEOMETRY, CONNECTION
from test_composer_geometry import DEVICE, frame

def literal_assignments(path, wanted):
    result = {}
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in wanted:
                    try:
                        result[target.id] = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        pass
    return result

catalog = copy.deepcopy(MENU['console']['catalog'])
examples = literal_assignments(ROOT / 'backend/orchestrator/welcome.py', ['WELCOME_EXAMPLES'])['WELCOME_EXAMPLES']
categories = literal_assignments(ROOT / 'backend/orchestrator/web_landing.py', ['SCENARIO_CATEGORIES', 'CATEGORY_ORDER'])
catalog['scenarios'] = []
for title, caption, query in examples:
    slug = '_'.join(re.findall(r'[a-z0-9]+', title.lower()))
    catalog['scenarios'].append(dict(id=slug, title=title, description=caption, prompt=query, category=categories['SCENARIO_CATEGORIES'].get(slug, 'Utilities')))
catalog['categories'] = list(categories['CATEGORY_ORDER'])
catalog['agents'] = []
for path in (ROOT / 'backend/agents').glob('*/*_agent.py'):
    values = literal_assignments(path, ['agent_id', 'service_name', 'description'])
    if set(values) == {'agent_id', 'service_name', 'description'}:
        catalog['agents'].append(dict(id=values['agent_id'], name=values['service_name'], description=values['description'], state='ready', owned=False))
catalog['agents'].sort(key=lambda row: row['name'].lower())
menu = copy.deepcopy(MENU)
menu['console']['catalog'] = catalog
spec = importlib.util.spec_from_file_location('visual_composer', PROJECTION / 'backend/webrender/chrome/composer_model.py')
composer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = composer
spec.loader.exec_module(composer)
out = ROOT / 'build/windows-091/visual-iteration'
out.mkdir(exist_ok=True)
application = QApplication([])
appmod.configure(application)
settings = QSettings(str(out / 'diagnostic.ini'), QSettings.Format.IniFormat)
with patch.object(appmod, 'OrchestratorClient', _FakeClient), patch.object(appmod, 'QSettings', lambda *a, **k: settings), patch.object(appmod.MainWindow, '_start_integrity_check', lambda self: None), patch.object(appmod.MainWindow, '_init_workspace', lambda self: None), patch.object(appmod, 'load_or_create_voice_device_id', lambda: DEVICE):
    window = appmod.MainWindow('ws://127.0.0.1:9/ws', '', connect=False)
    window.client.connection_generation = CONNECTION
    window._continuity.bind_connection(CONNECTION)
    window._on_message({'type':'chrome_menu','model':menu})
    window._on_message({'type':'rote_config','device_profile':{'console':GEOMETRY[5]['presentation']}})
    window.show()
    window._voice_widget.apply_composer_state(frame(composer), CONNECTION)
    results = []
    for geometry in [row for row in reversed(GEOMETRY) if row['viewport'] in [[1440,900],[1280,800],[1024,768],[834,1194],[768,1024],[390,844],[320,740]]]:
        width,height = geometry['viewport']
        window.resize(width,height)
        window._on_message({'type':'rote_config','device_profile':{'console':geometry['presentation']}})
        for _ in range(8):
            QApplication.processEvents()
            QTest.qWait(20)
        shell = window._console_shell
        shell._size_controls()
        window._input.clearFocus()
        QApplication.processEvents()
        assert (window.width(),window.height()) == (width,height)
        window.grab().save(str(out / f'landing-{width}x{height}.png'))
        widgets = {name: getattr(shell,name) for name in ['sidebar','header','title','subtitle','categories_scroll','scenarios','composer']}
        positions = {name:[widget.mapTo(window, widget.rect().topLeft()).x(),widget.mapTo(window,widget.rect().topLeft()).y(),widget.width(),widget.height()] for name,widget in widgets.items()}
        results.append({'size':[width,height],'widgets':positions})
    (out / 'geometry.json').write_text(json.dumps(results,indent=2))
    window.close()


