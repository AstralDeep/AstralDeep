"""Measures installed Qt font matching and advances against the shared Open Sans face."""

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / "components/AstralProjection/windows-client"))
from PySide6 import __version__
from PySide6.QtGui import QFont, QFontDatabase, QFontInfo, QFontMetricsF
from PySide6.QtWidgets import QApplication, QLabel
from astral_client import theme

app = QApplication([])
theme.configure_fonts(app)
app.setStyleSheet(theme.build_stylesheet())
text = "CPU, memory and disk read live from the host and laid out as KPI tiles and gauges."
rows = []
for family in [theme.FONT, "'Open Sans'", "'Segoe UI'"]:
    for hint in [QFont.HintingPreference.PreferDefaultHinting, QFont.HintingPreference.PreferNoHinting,
                 QFont.HintingPreference.PreferVerticalHinting, QFont.HintingPreference.PreferFullHinting]:
        for size, weight in [(12,400),(14,400),(24,800)]:
            label = QLabel(text if size != 24 else "AstralDeep Console")
            font = QFont("Open Sans")
            font.setPixelSize(size)
            font.setWeight(QFont.Weight(weight))
            font.setHintingPreference(hint)
            label.setFont(font)
            label.setStyleSheet(f"font-family:{family};font-size:{size}px;font-weight:{weight};")
            label.ensurePolished()
            resolved = QFontInfo(label.font())
            metrics = QFontMetricsF(label.font())
            rows.append(dict(family=family,hint=hint.name,size=size,weight=weight,
                             resolved_family=resolved.family(),resolved_style=resolved.styleName(),
                             resolved_weight=resolved.weight(),width=metrics.horizontalAdvance(label.text()),
                             ascent=metrics.ascent(),descent=metrics.descent(),height=metrics.height(),
                             line_spacing=metrics.lineSpacing()))
report = {"qt_binding":__version__,"styles":QFontDatabase.styles("Open Sans"),"measurements":rows}
path = root / "build/windows-091/font-probe.json"
path.write_text(json.dumps(report,indent=2))
print(json.dumps(report,indent=2))
