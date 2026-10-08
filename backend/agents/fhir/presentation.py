"""Builds the astralprims cards for patient measurements, population queries and FHIR clinical dashboards.
mcp_tools.py passes in view models from clinical.py and returns each card's dict.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from astralprims import (
    ActionGroup,
    Alert,
    Button,
    Card,
    Gauge,
    Grid,
    Hero,
    KeyValue,
    MetricCard,
    PlotlyChart,
    StatGroup,
    Table,
    Text,
    Timeline,
)

from agents.fhir import clinical

GRID_COLOR = "rgba(148,163,184,0.18)"
BAND_COLOR = "rgba(16,185,129,0.10)"
REFERENCE_COLOR = "rgba(148,163,184,0.85)"
LEGEND = {"orientation": "h", "y": -0.22}
SPARSE_POINTS = 24
LIVE_MINUTES = 5
VITAL_TILES = (
    "heart_rate", "oxygen_saturation", "respiratory_rate", "mean_arterial_pressure",
    "systolic", "diastolic", "temperature", "glasgow_coma_score",
)
DISCLAIMER = (
    "Synthetic and public de-identified reference demonstration data. "
    "Flags use fixed display thresholds and are not clinical advice."
)


def plot_time(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M")


def ask(label: str, message: str, variant: str = "secondary") -> Button:
    return Button(label=label, action="chat_message", payload={"message": message}, variant=variant)


def refresh(label: str = "Refresh") -> Button:
    return Button(label=label, action="component_action", payload={"kind": "refresh"}, variant="primary")


def go_live(label: str, tool: str, parameters: Dict[str, Any]) -> Button:
    return Button(label=label, action="stream_subscribe", payload={"tool_name": tool, "params": parameters}, variant="secondary")


def axis(title: str, **extra: Any) -> Dict[str, Any]:
    return {"title": title, "gridcolor": GRID_COLOR, "zeroline": False, **extra}


def band(low: float, high: float, reference: str = "y") -> Dict[str, Any]:
    return {
        "type": "rect", "xref": "paper", "x0": 0, "x1": 1, "yref": reference, "y0": low, "y1": high,
        "fillcolor": BAND_COLOR, "line": {"width": 0}, "layer": "below",
    }


def reference_line(level: float) -> Dict[str, Any]:
    return {
        "type": "line", "xref": "paper", "x0": 0, "x1": 1, "yref": "y", "y0": level, "y1": level,
        "line": {"color": REFERENCE_COLOR, "width": 1, "dash": "dot"}, "layer": "below",
    }


def reference_note(level: float, text: str) -> Dict[str, Any]:
    return {
        "xref": "paper", "x": 1, "xanchor": "right", "yref": "y", "y": level, "yanchor": "bottom", "text": text,
        "showarrow": False, "font": {"size": 10, "color": REFERENCE_COLOR},
    }


def line(label: str, points: Sequence[Tuple[datetime, float]], color: str, **extra: Any) -> Dict[str, Any]:
    return {
        "type": "scatter", "mode": "lines+markers" if len(points) <= SPARSE_POINTS else "lines", "name": label,
        "x": [plot_time(when) for when, _ in points], "y": [value for _, value in points],
        "line": {"color": color, "width": 2}, "marker": {"color": color, "size": 5}, **extra,
    }


def lab_table(rows: List[Dict[str, Any]], title: str) -> Table:
    changes = any(row["change"] for row in rows)
    headers = ["Test", "Result", "Reference", "Flag"] + (["Change"] if changes else []) + ["Collected"]
    return Table(
        headers=headers,
        rows=[
            [row["label"], f"{row['value']} {row['unit']}".strip(), row["reference"], row["flag"]]
            + ([row["change"]] if changes else []) + [clinical.format_time(row["when"])]
            for row in rows
        ],
        attributes={"title": title},
    )


def vital_tiles(latest: Dict[str, clinical.Reading], now: Optional[datetime], keys: Sequence[str]) -> List[MetricCard]:
    tiles = []
    for key in keys:
        item = latest.get(key)
        vital = clinical.VITALS_BY_KEY[key]
        if item is None:
            tiles.append(MetricCard(title=vital.label, value="—", subtitle="no reading"))
            continue
        tiles.append(MetricCard(
            title=vital.label,
            value=f"{item.display} {vital.unit}".strip(),
            subtitle=clinical.ago(item.when, now) or clinical.format_time(item.when),
            variant=vital.variant(item.value),
        ))
    return tiles


def census_card(
    as_of: Optional[datetime],
    total: int,
    rows: List[Dict[str, Any]],
    units: List[Tuple[str, int]],
    admissions: List[Tuple[datetime, int]],
    admitted_last_hour: int,
    live: bool = False,
) -> Card:
    critical = sum(1 for row in rows if row["flag"] == "Critical")
    badges = [f"{admitted_last_hour} admitted in the last hour", f"{len(units)} unit types"]
    if critical:
        badges.append(f"{critical} of {len(rows)} shown flagged critical")
    content: List[Any] = [
        Hero(
            eyebrow="Live FHIR R5 feed",
            title=f"{total} patients in intensive care",
            subtitle=f"As of {clinical.format_time(as_of)}",
            variant="gradient",
            badges=badges,
        ),
        StatGroup(title="Census by unit type", columns=4, items=[{"label": unit, "value": str(count)} for unit, count in units[:8]]),
    ]
    if admissions:
        content.append(PlotlyChart(
            title="ICU admissions per hour, last 24 hours",
            data=[{
                "type": "bar", "name": "Admissions", "x": [plot_time(when) for when, _ in admissions],
                "y": [count for _, count in admissions], "marker": {"color": "#6366F1"},
            }],
            layout={"xaxis": axis("", type="date"), "yaxis": axis("Admissions", rangemode="tozero"), "showlegend": False, "bargap": 0.15},
        ))
    content.append(Table(
        headers=["Patient", "Vitals", "HR", "SpO2", "MAP", "Unit", "In unit", "Admission diagnosis"],
        rows=[
            [row["patient"], row["flag"] or "—", row["heart_rate"], row["oxygen_saturation"], row["mean_arterial_pressure"],
             row["unit"] or "—", row["stay"] or "—", row["reason"] or "—"]
            for row in rows
        ],
        attributes={"title": "Most recently admitted"},
    ))
    buttons = [refresh("Refresh census")]
    if live:
        buttons.append(go_live("Watch live feed", "watch_icu_activity", {"minutes": LIVE_MINUTES}))
    for row in [row for row in rows if row["flag"] == "Critical"][:2] or rows[:1]:
        buttons.append(ask(f"Open patient {row['patient']}", f"Show the FHIR patient overview for patient {row['patient']}"))
    content.append(ActionGroup(buttons=buttons, label="Next"))
    content.append(Text(content=DISCLAIMER, variant="caption"))
    return Card(title="ICU census", id="fhir-icu-census", content=content)


def patient_card(
    patient_id: str,
    headline: str,
    eyebrow: str,
    badges: List[str],
    latest: Dict[str, clinical.Reading],
    now: Optional[datetime],
    facts: List[Dict[str, str]],
    risks: List[Tuple[str, float]],
    problems: List[List[str]],
    medications: List[List[str]],
    labs: List[Dict[str, Any]],
    allergies: List[str],
    live: bool = False,
) -> Card:
    content: List[Any] = [
        Hero(eyebrow=eyebrow, title=f"Patient {patient_id}", subtitle=headline, variant="gradient", badges=badges),
        Grid(columns=4, children=vital_tiles(latest, now, VITAL_TILES)) if latest
        else Alert(message="No vital signs have been charted for this patient yet.", variant="info"),
        KeyValue(title="Stay", items=facts, columns=3),
    ]
    if risks:
        content.append(Grid(columns=len(risks), children=[
            Gauge(
                label=label, value=max(0.0, min(1.0, probability)), display_value=f"{probability * 100:.1f}%",
                subtitle="APACHE prediction",
                thresholds=[{"at": 0.0, "variant": "default"}, {"at": 0.2, "variant": "warning"}, {"at": 0.5, "variant": "error"}],
            )
            for label, probability in risks
        ]))
    if allergies:
        content.append(Alert(title="Allergies", message=", ".join(allergies), variant="warning"))
    if problems:
        content.append(Table(headers=["Problem", "Status", "Recorded"], rows=problems, attributes={"title": "Problems"}))
    if labs:
        content.append(lab_table(labs, "Key laboratory results"))
    if medications:
        content.append(Table(headers=["Medication", "Dose and schedule", "Started"], rows=medications, attributes={"title": "Active orders"}))
    explore = [
        ask("Vital sign trends", f"Chart the vital sign trends for FHIR patient {patient_id}", "primary"),
        ask("Laboratory results", f"Show the laboratory results for FHIR patient {patient_id}"),
        ask("Medications", f"Review the medications for FHIR patient {patient_id}"),
        ask("Timeline", f"Show the clinical timeline for FHIR patient {patient_id}"),
    ]
    if live:
        explore.insert(1, go_live("Stream live vitals", "stream_patient_vitals", {"patient": patient_id, "minutes": LIVE_MINUTES}))
    content.append(ActionGroup(label="Explore", buttons=explore))
    content.append(Text(content=DISCLAIMER, variant="caption"))
    return Card(title=f"Patient {patient_id}", id=f"fhir-patient-{patient_id}", content=content)


def trend_charts(traces: Dict[str, List[Tuple[datetime, float]]]) -> List[Any]:
    drawn = {key: points for key, points in traces.items() if len(points) >= 2}
    charts: List[Any] = []
    rates = [
        line(clinical.VITALS_BY_KEY[key].label, drawn[key], clinical.VITALS_BY_KEY[key].color)
        for key in ("heart_rate", "respiratory_rate") if key in drawn
    ]
    saturation = drawn.get("oxygen_saturation")
    if rates or saturation:
        layout: Dict[str, Any] = {"xaxis": axis("", type="date"), "hovermode": "x unified", "legend": LEGEND}
        if saturation:
            vital = clinical.VITALS_BY_KEY["oxygen_saturation"]
            lowest = min(85.0, math.floor(min(value for _, value in saturation)) - 2.0)
            scale = {"title": "SpO2 %", "range": [lowest, 101], "zeroline": False}
            if rates:
                rates.append(line(vital.label, saturation, vital.color, yaxis="y2"))
                layout["yaxis"] = axis("per minute")
                layout["yaxis2"] = {**scale, "overlaying": "y", "side": "right", "showgrid": False, "automargin": True}
            else:
                rates.append(line(vital.label, saturation, vital.color))
                layout["yaxis"] = {**scale, "gridcolor": GRID_COLOR}
        else:
            layout["yaxis"] = axis("per minute")
        charts.append(PlotlyChart(title=" · ".join(trace["name"] for trace in rates), data=rates, layout=layout))
    pressure = [
        line(clinical.VITALS_BY_KEY[key].label, drawn[key], clinical.VITALS_BY_KEY[key].color)
        for key in ("systolic", "mean_arterial_pressure", "diastolic") if key in drawn
    ]
    if pressure:
        target = clinical.VITALS_BY_KEY["mean_arterial_pressure"].low_warning
        charts.append(PlotlyChart(
            title="Blood pressure",
            data=pressure,
            layout={
                "xaxis": axis("", type="date"), "yaxis": axis("mmHg"), "hovermode": "x unified", "legend": LEGEND,
                "shapes": [reference_line(target)], "annotations": [reference_note(target, f"MAP {target:g}")],
            },
        ))
    if "temperature" in drawn:
        vital = clinical.VITALS_BY_KEY["temperature"]
        charts.append(PlotlyChart(
            title="Temperature",
            data=[line(vital.label, drawn["temperature"], vital.color)],
            layout={
                "xaxis": axis("", type="date"), "yaxis": axis("°C"), "showlegend": False,
                "shapes": [band(vital.low_warning, vital.high_warning)],
            },
        ))
    return charts


def vitals_card(
    patient_id: str,
    hours: int,
    now: Optional[datetime],
    summary: List[Dict[str, str]],
    traces: Dict[str, List[Tuple[datetime, float]]],
    note: str = "",
    live: bool = False,
) -> Card:
    content: List[Any] = [
        Hero(
            eyebrow="Vital sign trends", title=f"Patient {patient_id}",
            subtitle=f"Last {hours} hours · as of {clinical.format_time(now)}", variant="subtle",
        ),
    ]
    if summary:
        content.append(StatGroup(title="Latest, with range over the window", columns=4, items=summary))
    charts = trend_charts(traces)
    content += charts
    if not traces:
        content.append(Alert(message=f"No vital signs were recorded for this patient in the last {hours} hours.", variant="info"))
    elif not charts:
        content.append(Alert(message=f"Too few readings in the last {hours} hours to draw a trend.", variant="info"))
    if note:
        content.append(Text(content=note, variant="caption"))
    buttons = [refresh(), ask("Patient overview", f"Show the FHIR patient overview for patient {patient_id}")]
    if live:
        buttons.insert(1, go_live("Stream live", "stream_patient_vitals", {"patient": patient_id, "minutes": LIVE_MINUTES}))
    content.append(ActionGroup(label="Next", buttons=buttons))
    content.append(Text(
        content="The dotted line and the shaded band mark typical adult targets. " + DISCLAIMER, variant="caption",
    ))
    return Card(title=f"Vital signs: patient {patient_id}", id=f"fhir-vitals-{patient_id}", content=content)


def live_vitals_card(
    patient_id: str,
    watching: bool,
    minutes: int,
    fresh: int,
    latest: Dict[str, clinical.Reading],
    now: Optional[datetime],
    traces: Dict[str, List[Tuple[datetime, float]]],
) -> Card:
    state = f"Streaming for {minutes} min" if watching else "Stream ended"
    content: List[Any] = [
        Hero(
            eyebrow="Live FHIR stream", title=f"Patient {patient_id}",
            subtitle=f"{state} · as of {clinical.format_time(now)}", variant="gradient",
            badges=[f"{clinical.count_of(fresh, 'new reading')} since the stream began", "Updates as readings arrive"],
        ),
        Grid(columns=4, children=vital_tiles(latest, now, VITAL_TILES)) if latest
        else Alert(message="No vital signs have been charted for this patient yet.", variant="info"),
    ]
    content += trend_charts(traces)
    buttons = [ask("Patient overview", f"Show the FHIR patient overview for patient {patient_id}")]
    if not watching:
        buttons.insert(0, go_live("Stream again", "stream_patient_vitals", {"patient": patient_id, "minutes": minutes}))
    content.append(ActionGroup(label="Next", buttons=buttons))
    content.append(Text(content=DISCLAIMER, variant="caption"))
    return Card(title=f"Live vitals: patient {patient_id}", content=content)


def labs_card(patient_id: str, hours: int, now: Optional[datetime], rows: List[Dict[str, Any]]) -> Card:
    abnormal = [row for row in rows if row["flag"] in ("Critical", "Moderate")]
    content: List[Any] = [
        Hero(
            eyebrow="Laboratory results", title=f"Patient {patient_id}",
            subtitle=f"Last {hours} hours · as of {clinical.format_time(now)}", variant="subtle",
            badges=[f"{len(rows)} tests", f"{len(abnormal)} outside reference"],
        ),
    ]
    if not rows:
        content.append(Alert(message=f"No laboratory results were reported for this patient in the last {hours} hours.", variant="info"))
    else:
        content.append(lab_table(rows, "Latest result per test"))
        charts = []
        for row in [row for row in rows if len(row["history"]) >= 2][:4]:
            interval = clinical.LAB_RANGES.get(row["key"])
            shapes = [band(interval.low, interval.high)] if interval and interval.low is not None and interval.high is not None else []
            charts.append(PlotlyChart(
                title=f"{row['label']} ({row['unit']})" if row["unit"] else row["label"],
                data=[line(row["label"], row["history"], "#6366F1")],
                layout={"xaxis": axis("", type="date"), "yaxis": axis(row["unit"]), "shapes": shapes, "showlegend": False},
            ))
        if charts:
            content.append(Grid(columns=2, children=charts))
    content.append(ActionGroup(label="Next", buttons=[
        refresh(), ask("Patient overview", f"Show the FHIR patient overview for patient {patient_id}"),
    ]))
    content.append(Text(content="Reference intervals are typical adult values. " + DISCLAIMER, variant="caption"))
    return Card(title=f"Laboratory results: patient {patient_id}", id=f"fhir-labs-{patient_id}", content=content)


def medications_card(
    patient_id: str,
    now: Optional[datetime],
    counts: Dict[str, int],
    orders: List[List[str]],
    infusions: Dict[str, List[Tuple[datetime, float]]],
    infusion_units: Dict[str, str],
    home: List[List[str]],
    notes: Dict[str, str],
) -> Card:
    content: List[Any] = [
        Hero(
            eyebrow="Medication review", title=f"Patient {patient_id}",
            subtitle=f"As of {clinical.format_time(now)}", variant="subtle",
        ),
        StatGroup(columns=4, items=[
            {"label": "Active orders", "value": str(counts.get("active", 0))},
            {"label": "Completed", "value": str(counts.get("completed", 0))},
            {"label": "Cancelled", "value": str(counts.get("cancelled", 0))},
            {"label": "Home medications", "value": str(len(home))},
        ]),
    ]
    if orders:
        content.append(Table(headers=["Medication", "Dose and schedule", "Status", "Ordered"], rows=orders, attributes={"title": "Orders"}))
    if notes.get("orders"):
        content.append(Text(content=notes["orders"], variant="caption"))
    palette = ("#EF4444", "#3B82F6", "#10B981", "#F59E0B", "#A855F7", "#14B8A6")
    units = {infusion_units.get(name, "") for name in infusions}
    shared = units.pop() if len(units) == 1 else ""
    traces = [
        line(name if shared or not infusion_units.get(name) else f"{name} ({infusion_units[name]})", points, palette[index % len(palette)],
             line={"color": palette[index % len(palette)], "width": 2, "shape": "hv"})
        for index, (name, points) in enumerate(infusions.items())
    ]
    if traces:
        content.append(PlotlyChart(
            title=f"Charted infusion rates ({shared})" if shared else "Charted infusion rates",
            data=traces,
            layout={"xaxis": axis("", type="date"), "yaxis": axis(shared, rangemode="tozero"), "showlegend": True, "legend": LEGEND},
        ))
    if notes.get("infusions"):
        content.append(Text(content=notes["infusions"], variant="caption"))
    if home:
        content.append(Table(headers=["Home medication", "Dose"], rows=home, attributes={"title": "Before admission"}))
    if not orders and not traces and not home:
        content.append(Alert(message="No medication records are available for this patient yet.", variant="info"))
    content.append(ActionGroup(label="Next", buttons=[
        refresh(), ask("Patient overview", f"Show the FHIR patient overview for patient {patient_id}"),
    ]))
    content.append(Text(content=DISCLAIMER, variant="caption"))
    return Card(title=f"Medications: patient {patient_id}", id=f"fhir-medications-{patient_id}", content=content)


def timeline_card(patient_id: str, hours: int, now: Optional[datetime], events: List[Dict[str, Any]], total: int) -> Card:
    content: List[Any] = [
        Hero(
            eyebrow="Clinical timeline", title=f"Patient {patient_id}",
            subtitle=f"Last {hours} hours · as of {clinical.format_time(now)}", variant="subtle",
            badges=[f"{total} events"],
        ),
    ]
    if events:
        content.append(Timeline(items=[
            {
                "title": event["title"], "time": clinical.format_time(event["when"]),
                **({"description": event["description"]} if event["description"] else {}), "variant": event["variant"],
            }
            for event in events
        ]))
    else:
        content.append(Alert(message=f"Nothing was recorded for this patient in the last {hours} hours.", variant="info"))
    if total > len(events):
        content.append(Text(content=f"Showing the {len(events)} most recent of {total} events.", variant="caption"))
    content.append(ActionGroup(label="Next", buttons=[
        refresh(), ask("Patient overview", f"Show the FHIR patient overview for patient {patient_id}"),
    ]))
    content.append(Text(content=DISCLAIMER, variant="caption"))
    return Card(title=f"Timeline: patient {patient_id}", id=f"fhir-timeline-{patient_id}", content=content)


def source_card(
    title: str,
    subtitle: str,
    badges: List[str],
    stats: List[Dict[str, str]],
    facts: List[Dict[str, str]],
    resources: List[List[str]],
) -> Card:
    content: List[Any] = [
        Hero(eyebrow="FHIR data source", title=title, subtitle=subtitle, variant="gradient", badges=badges),
    ]
    if stats:
        content.append(StatGroup(columns=4, items=stats))
    content.append(KeyValue(title="Server", items=facts, columns=2))
    if resources:
        content.append(Table(headers=["Resource", "Interactions", "Search parameters"], rows=resources, attributes={"title": "What can be queried"}))
    content.append(ActionGroup(label="Try", buttons=[
        ask("ICU census", "Show the current ICU census from the FHIR feed", "primary"),
        ask("Watch the live feed", "Watch the live ICU activity feed for two minutes"),
    ]))
    return Card(title="FHIR data source", id="fhir-source", content=content)


def records_card(resource_type: str, headers: List[str], rows: List[List[str]], total: Optional[int], query: str) -> Card:
    shown = f"Showing {len(rows)}" + (f" of {total}" if total is not None and total > len(rows) else "")
    content: List[Any] = [
        Hero(eyebrow="FHIR search", title=f"{resource_type} records", subtitle=f"{shown} · {query or 'no filters'}", variant="subtle"),
    ]
    if rows:
        content.append(Table(headers=headers, rows=rows, attributes={"title": f"{resource_type} results"}))
    else:
        content.append(Alert(message="No records matched this search.", variant="info"))
    content.append(Text(content=DISCLAIMER, variant="caption"))
    return Card(title=f"FHIR search: {resource_type}", id=f"fhir-records-{resource_type.lower()}", content=content)


def measurements_card(data: Dict[str, Any]) -> Card:
    origin = {"synthetic": "Synthetic demonstration data", "public-deidentified": "Public de-identified demonstration data"}.get(
        data["data_origin"], "Data provenance is not fully established",
    )
    content: List[Any] = [Hero(
        eyebrow="Patient-level FHIR REST", title=data["patient_label"], subtitle=f"Patient {data['patient']}",
        variant="subtle", badges=[origin],
    )]
    if data["measurements"]:
        content.append(Table(
            headers=["Measurement", "Value", "Unit", "Observation date", "Date field", "Source"],
            rows=[[row["label"], clinical.format_number(row["value"]), row["unit"], row["measured_at"], row["date_basis"], row["source"]]
                  for row in data["measurements"]],
            attributes={"title": "Latest usable measurement"},
        ))
    else:
        content.append(Alert(message="No usable measurement was found in the returned records.", variant="info"))
    if data["limited"]:
        content.append(Text(content=f"This search inspected at most {data['observation_limit']} observations; additional records may exist.", variant="caption"))
    content.append(Text(
        content=f"{data['definition']}. {origin}. This reference result is not clinical advice or evidence about the Kentucky population.",
        variant="caption",
    ))
    content.append(ActionGroup(buttons=[refresh()], label="Patient measurement"))
    return Card(title="Patient measurements", id=f"fhir-patient-measurements-{data['patient']}-{data['measure']}", content=content)


def population_card(data: Dict[str, Any]) -> Card:
    scope = data["scope"]
    filters = [scope["state"] or "All states", ", ".join(scope["counties"]) or "All counties"]
    if scope["pregnant"] is not None:
        filters.append("Explicitly pregnant" if scope["pregnant"] else "Explicitly not pregnant")
    content: List[Any] = [
        Hero(eyebrow="Population-level SQL on FHIR", title="Average latest A1C by county", subtitle=" · ".join(filters),
             variant="subtle", badges=["Synthetic demonstration data"]),
    ]
    if data["rows"]:
        content.append(Table(
            headers=["County", "Cohort patients", "Patients with A1C", "Average latest A1C (%)"],
            rows=[[row["county"], str(row["patient_count"]), str(row["with_a1c_count"]),
                   clinical.format_number(row["average_a1c"]) if row["average_a1c"] is not None else "Not recorded"]
                  for row in data["rows"]],
            attributes={"title": "Synthetic population results"},
        ))
    else:
        content.append(Alert(message="No synthetic patients matched this cohort.", variant="info"))
    content.append(KeyValue(title="Calculation provenance", items=[
        {"label": "Source", "value": data["source"]}, {"label": "Generated", "value": data["generated_at"]},
        {"label": "Definition", "value": data["definition"]},
    ], columns=1))
    content.append(Text(content="Synthetic demo records only; these results do not describe actual Kentucky residents or KHIE data.", variant="caption"))
    content.append(ActionGroup(buttons=[refresh()], label="Population query"))
    return Card(title="Population A1C", id="fhir-population-a1c", content=content)


def feed_card(
    now: Optional[datetime],
    watching: bool,
    counters: Dict[str, int],
    events: List[Dict[str, Any]],
    recent: List[List[str]],
) -> Card:
    state = "Listening" if watching else "Finished"
    content: List[Any] = [
        Hero(
            eyebrow="Live FHIR subscription", title="ICU activity feed",
            subtitle=f"{state} · as of {clinical.format_time(now)}", variant="gradient",
            badges=[f"{counters.get('events', 0)} notifications", "R5 topic subscriptions"],
        ),
        StatGroup(columns=4, items=[
            {"label": "Admissions", "value": str(counters.get("admissions", 0))},
            {"label": "Discharges", "value": str(counters.get("discharges", 0))},
            {"label": "New results", "value": str(counters.get("observations", 0))},
            {"label": "Flagged readings", "value": str(counters.get("flagged", 0)),
             "variant": "error" if counters.get("flagged") else "default"},
        ]),
    ]
    if events:
        content.append(Timeline(title="Notable events", items=[
            {
                "title": event["title"], "time": clinical.format_time(event["when"]),
                **({"description": event["description"]} if event["description"] else {}), "variant": event["variant"],
            }
            for event in events
        ]))
    if recent:
        content.append(Table(headers=["Time", "Patient", "Measurement", "Value"], rows=recent, attributes={"title": "Latest results"}))
    if not events and not recent:
        content.append(Alert(message="Waiting for the next notifications from the feed.", variant="info"))
    content.append(Text(content=DISCLAIMER, variant="caption"))
    return Card(title="ICU activity feed", content=content)
