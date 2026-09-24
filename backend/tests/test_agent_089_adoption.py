"""Tests for the weather agent's adoption of astralprims stat-group and gauge components
(mcp_tools.py, rote/adapter.py): payload preservation and the non-web degrade path
through rote/capabilities.py.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agents.weather.mcp_tools import _fraction  # noqa: E402


@pytest.mark.parametrize(
    "reading,expected",
    [
        (0, 0.0), (62, 0.62), (100, 1.0),
        ("62", 0.62), (62.5, 0.625),
        (150, 1.0), (-5, 0.0),
        (None, 0.0), ("N/A", 0.0), ("", 0.0),
        (float("nan"), 0.0),
    ],
)
def test_a_percentage_becomes_a_bounded_fraction(reading, expected) -> None:
    assert _fraction(reading) == pytest.approx(expected)


def test_a_missing_reading_never_raises() -> None:
    for reading in (None, "N/A", object(), [], {}):
        assert _fraction(reading) == 0.0


def _current_conditions_components():
    from astralprims import Gauge, StatGroup

    current = {
        "temperature_2m": 68,
        "apparent_temperature": 66,
        "relative_humidity_2m": 62,
        "wind_speed_10m": 8,
        "wind_direction_10m": 210,
        "pressure_msl": 1014,
    }
    stats = StatGroup(
        title="Current conditions",
        columns=4,
        id="current-conditions",
        items=[
            {"label": "Temperature", "value": f"{current['temperature_2m']}°F",
             "hint": f"Feels like {current['apparent_temperature']}°F",
             "variant": "default"},
            {"label": "Wind", "value": f"{current['wind_speed_10m']} mph",
             "hint": f"Direction {current['wind_direction_10m']}°"},
            {"label": "Pressure", "value": f"{current['pressure_msl']} hPa"},
        ],
    )
    gauge = Gauge(
        label="Humidity",
        value=_fraction(current["relative_humidity_2m"]),
        display_value=f"{current['relative_humidity_2m']}%",
        id="humidity-gauge",
        thresholds=[
            {"at": 0.0, "variant": "default"},
            {"at": 0.70, "variant": "warning"},
            {"at": 0.90, "variant": "error"},
        ],
    )
    return stats.to_dict(), gauge.to_dict()


def test_the_readings_are_preserved_in_the_new_shape() -> None:
    stats, gauge = _current_conditions_components()
    rendered = str(stats) + str(gauge)
    for reading in ("68°F", "66°F", "8 mph", "210°", "1014 hPa", "62%"):
        assert reading in rendered


def test_the_gauge_value_matches_its_display_value() -> None:
    _stats, gauge = _current_conditions_components()
    assert gauge["value"] == pytest.approx(0.62)
    assert gauge["display_value"] == "62%"


def test_the_gauge_thresholds_ascend() -> None:
    _stats, gauge = _current_conditions_components()
    ats = [t["at"] for t in gauge["thresholds"]]
    assert ats == sorted(ats)


def test_neither_component_carries_a_color() -> None:
    stats, gauge = _current_conditions_components()
    rendered = str(stats) + str(gauge)
    assert "#" not in rendered
    assert "rgb" not in rendered


def test_the_stat_group_degrades_to_the_grid_of_metrics_it_replaced() -> None:
    pytest.importorskip("rote.adapter")
    from rote.adapter import ComponentAdapter
    from rote.capabilities import DeviceProfile

    stats, _gauge = _current_conditions_components()
    adapted = ComponentAdapter.adapt([stats], DeviceProfile.from_dict(
        {"device_type": "windows"}))
    assert adapted[0]["type"] == "grid"
    assert [c["type"] for c in adapted[0]["children"]] == ["metric"] * 3
    assert adapted[0]["children"][0]["title"] == "Temperature"


def test_the_gauge_degrades_to_a_progress_bar() -> None:
    pytest.importorskip("rote.adapter")
    from rote.adapter import ComponentAdapter
    from rote.capabilities import DeviceProfile

    _stats, gauge = _current_conditions_components()
    adapted = ComponentAdapter.adapt([gauge], DeviceProfile.from_dict(
        {"device_type": "windows"}))
    assert adapted[0]["type"] == "progress"
    assert adapted[0]["value"] == pytest.approx(0.62)
    assert "62%" in adapted[0]["label"]


def test_identity_survives_so_canvas_morphs_still_work() -> None:
    pytest.importorskip("rote.adapter")
    from rote.adapter import ComponentAdapter
    from rote.capabilities import DeviceProfile

    stats, gauge = _current_conditions_components()
    adapted = ComponentAdapter.adapt(
        [stats, gauge], DeviceProfile.from_dict({"device_type": "android"})
    )
    assert [c.get("id") for c in adapted] == ["current-conditions", "humidity-gauge"]
