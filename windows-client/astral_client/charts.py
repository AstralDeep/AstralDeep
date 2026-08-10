"""QtCharts renderers for the bar/line/pie SDUI chart primitives. Plotly specs
(`plotly_chart`) are web-only and fall back to a placeholder in renderer.py."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtCharts import (
    QChart,
    QChartView,
    QBarSeries,
    QBarSet,
    QBarCategoryAxis,
    QValueAxis,
    QLineSeries,
    QPieSeries,
)

from . import theme as T

def _palette() -> list:
    """Series colors read from the LIVE theme (primary → secondary → accent,
    then the fixed semantic tokens). Computed per call, not captured at import:
    the palette is mutable (feature 044 US5), so a hardcoded list left charts
    indigo while the rest of the app switched to ocean/sunset/forest."""
    return [
        T.PRIMARY,
        T.SECONDARY,
        T.ACCENT,
        T.VARIANT_COLORS["success"][0],
        T.VARIANT_COLORS["warning"][0],
        T.VARIANT_COLORS["error"][0],
    ]


def _style(chart: QChart, title: str) -> QChartView:
    chart.setTitle(title or "")
    chart.setTitleBrush(QColor(T.TEXT))
    # Web .astral-chart-card: layered surface-1 + soft border + radius-md, NOT
    # the solid raised surface.
    chart.setBackgroundBrush(QColor(T.SURFACE))
    chart.setPlotAreaBackgroundVisible(False)
    chart.legend().setLabelColor(QColor(T.MUTED))
    view = QChartView(chart)
    view.setRenderHint(QPainter.RenderHint.Antialiasing)
    view.setMinimumHeight(260)
    view.setStyleSheet(
        f"background:{T.SURFACE}; border:1px solid {T.BORDER}; border-radius:10px;"
    )
    return view


def _axis_color(axis) -> None:
    axis.setLabelsColor(QColor(T.MUTED))
    axis.setGridLineColor(QColor(T.BORDER))
    axis.setLinePenColor(QColor(T.BORDER))


def build_chart(c: dict) -> Optional[QChartView]:
    kind = c.get("type")
    title = c.get("title", "")
    labels = [str(x) for x in (c.get("labels") or [])]
    palette = _palette()
    if kind == "pie_chart":
        series = QPieSeries()
        data = c.get("data") or []
        for i, v in enumerate(data):
            label = labels[i] if i < len(labels) else str(i)
            sl = series.append(f"{label} ({v})", float(v))
            sl.setColor(QColor(palette[i % len(palette)]))
            sl.setLabelColor(QColor(T.TEXT))
        chart = QChart()
        chart.addSeries(series)
        return _style(chart, title)

    datasets = c.get("datasets") or []
    if kind == "line_chart":
        chart = QChart()
        for di, ds in enumerate(datasets):
            line = QLineSeries()
            line.setName(str(ds.get("label", f"series {di + 1}")))
            line.setColor(QColor(palette[di % len(palette)]))
            for i, v in enumerate(ds.get("data") or []):
                line.append(float(i), float(v))
            chart.addSeries(line)
        chart.createDefaultAxes()
        for ax in chart.axes():
            _axis_color(ax)
        return _style(chart, title)

    # default: bar_chart
    series = QBarSeries()
    for di, ds in enumerate(datasets):
        bs = QBarSet(str(ds.get("label", f"series {di + 1}")))
        bs.setColor(QColor(palette[di % len(palette)]))
        for v in ds.get("data") or []:
            bs.append(float(v))
        series.append(bs)
    chart = QChart()
    chart.addSeries(series)
    if labels:
        ax = QBarCategoryAxis()
        ax.append(labels)
        _axis_color(ax)
        chart.addAxis(ax, Qt.AlignmentFlag.AlignBottom)
        series.attachAxis(ax)
    ay = QValueAxis()
    _axis_color(ay)
    chart.addAxis(ay, Qt.AlignmentFlag.AlignLeft)
    series.attachAxis(ay)
    return _style(chart, title)
