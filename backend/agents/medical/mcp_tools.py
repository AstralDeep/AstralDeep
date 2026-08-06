"""
MCP Tools for Medical Agent.
Includes tools for synthetic patient data generation, data analysis, and file handling primitives.
"""
import os
import sys
import random
import csv
import io
from typing import Dict, Any, List, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from astralprims import (
    Card, Table, Grid, MetricCard, Alert, Text,
    FileUpload, FileDownload, create_ui_response
)

# =============================================================================
# MOCK PATIENT DATA & TOOLS
# =============================================================================

MOCK_PATIENTS = [
    {"id": "P-1024", "name": "Sarah Connor", "age": 45, "condition": "Degenerative Disc Disease - L4/L5", "status": "Severe", "blood_pressure": "142/88", "heart_rate": 78},
    {"id": "P-1092", "name": "John Smith", "age": 38, "condition": "Degenerative Disc Disease - C5/C6", "status": "Moderate", "blood_pressure": "128/82", "heart_rate": 72},
    {"id": "P-1123", "name": "Emily Davis", "age": 52, "condition": "Degenerative Disc Disease - L5/S1", "status": "Critical", "blood_pressure": "156/94", "heart_rate": 88},
    {"id": "P-1245", "name": "Michael Brown", "age": 34, "condition": "Degenerative Disc Disease - L3/L4", "status": "Mild", "blood_pressure": "118/76", "heart_rate": 68},
    {"id": "P-1301", "name": "Lisa Wilson", "age": 41, "condition": "Osteoarthritis - Knee", "status": "Moderate", "blood_pressure": "132/84", "heart_rate": 74},
    {"id": "P-1388", "name": "Robert Taylor", "age": 55, "condition": "Chronic Back Pain", "status": "Severe", "blood_pressure": "148/90", "heart_rate": 82},
    {"id": "P-1402", "name": "Jennifer Lee", "age": 29, "condition": "Scoliosis", "status": "Mild", "blood_pressure": "112/72", "heart_rate": 66},
    {"id": "P-1455", "name": "David Martinez", "age": 47, "condition": "Spinal Stenosis", "status": "Moderate", "blood_pressure": "138/86", "heart_rate": 76},
    {"id": "P-1500", "name": "Amanda Clark", "age": 36, "condition": "Herniated Disc - L4/L5", "status": "Moderate", "blood_pressure": "124/78", "heart_rate": 70},
    {"id": "P-1567", "name": "James Anderson", "age": 62, "condition": "Degenerative Disc Disease - Multiple", "status": "Critical", "blood_pressure": "162/96", "heart_rate": 92},
]


def search_patients(min_age: int = 0, max_age: int = 200, condition: str = "", session_id: str = "default", **kwargs) -> Dict[str, Any]:
    """Search patients by age range and/or condition.

    Args:
        min_age: Minimum age filter (default: 0)
        max_age: Maximum age filter (default: 200)
        condition: Condition keyword to filter by (case-insensitive, partial match)

    Returns:
        Dict with _ui_components and _data keys.
    """
    try:
        min_age = int(min_age)
        max_age = int(max_age)
    except ValueError:
        pass

    results = []
    for p in MOCK_PATIENTS:
        if p["age"] < min_age or p["age"] > max_age:
            continue
        if condition and condition.lower() not in p["condition"].lower():
            continue
        results.append(p)

    if not results:
        return create_ui_response([
            Alert(message="No patients found matching your criteria.", variant="info", title="Search Results")
        ])

    headers = ["ID", "Name", "Age", "Condition", "Status"]
    rows = [[p["id"], p["name"], str(p["age"]), p["condition"], p["status"]] for p in results]

    status_summary = {}
    for p in results:
        status_summary[p["status"]] = status_summary.get(p["status"], 0) + 1

    components = [
        Card(
            title=f"Patient Search Results ({len(results)} found)",
            id="patient-results-card",
            content=[
                Text(content=f"Showing patients aged {min_age}+" +
                     (f" with condition matching '{condition}'" if condition else ""),
                     variant="caption"),
                Table(headers=headers, rows=rows, id="patient-table"),
            ]
        ),
        Grid(
            columns=len(status_summary),
            id="patient-metrics",
            children=[
                MetricCard(
                    title=status,
                    value=str(count),
                    subtitle="patients",
                    variant="warning" if status == "Severe" else "error" if status == "Critical" else "default",
                    id=f"metric-{status.lower()}"
                )
                for status, count in status_summary.items()
            ]
        )
    ]

    return {
        "_ui_components": [c.to_dict() for c in components],
        "_data": {"patients": results, "total": len(results)}
    }


def generate_synthetic_patients(count: int = 50, session_id: str = "default", user_id: str = "legacy", **kwargs) -> Dict[str, Any]:
    """Generate synthetic patient records and return a downloadable file component.

    Args:
        count: Number of patient records to generate.
    """
    conditions = ["Hypertension", "Type 2 Diabetes", "Asthma", "Osteoarthritis", "Healthy"]
    statuses = ["Stable", "Monitoring", "Critical", "Recovered"]
    
    patients = []
    for i in range(count):
        patients.append({
            "id": f"SYN-{1000 + i}",
            "age": random.randint(18, 90),
            "condition": random.choice(conditions),
            "status": random.choice(statuses),
            "heart_rate": random.randint(60, 100)
        })

    # Save to CSV
    filename = f"synthetic_patients_{count}.csv"
    
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    download_dir = os.path.join(backend_dir, "tmp", user_id, session_id)
    os.makedirs(download_dir, exist_ok=True)
    file_path = os.path.join(download_dir, filename)
    
    with open(file_path, mode='w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=["id", "age", "condition", "status", "heart_rate"])
        writer.writeheader()
        writer.writerows(patients)

    # Return a preview table and a FileDownload component
    headers = ["ID", "Age", "Condition", "Status", "Heart Rate"]
    rows = [[p["id"], str(p["age"]), p["condition"], p["status"], str(p["heart_rate"])] for p in patients[:5]]

    # Root-relative download URL — resolved against the serving origin by the
    # browser (Constitution X: no hard-coded localhost; feature 030).
    download_url = f"/api/download/{session_id}/{filename}"

    components = [
        Card(
            title="Synthetic Patient Data Generated",
            id="synth-data-card",
            content=[
                Alert(message=f"Successfully generated {count} synthetic patient records.", variant="success"),
                FileDownload(
                    label=f"Download {filename}",
                    url=download_url,
                    filename=filename
                ),
                Table(headers=headers, rows=rows, id="synth-data-preview"),
            ]
        )
    ]

    return {
        "_ui_components": [c.to_dict() for c in components],
        "_data": {"generated_count": count, "preview": patients[:5], "file_path": file_path}
    }


def analyze_patient_data() -> Dict[str, Any]:
    """Analyze patient data and ask the user to upload a file if more data is needed."""
    
    components = [
        Card(
            title="Patient Data Analysis",
            id="analysis-card",
            content=[
                Alert(message="Provide a patient dataset for comprehensive analysis.", variant="info"),
                FileUpload(
                    label="Upload Dataset CSV",
                    accept=".csv,.json",
                    action="analyze_uploaded_data"
                ),
                Grid(
                    columns=2,
                    children=[
                        MetricCard(title="Analysis Ready", value="Yes", variant="success"),
                        MetricCard(title="Supported Formats", value="CSV, JSON", variant="default")
                    ]
                )
            ]
        )
    ]

    return {
        "_ui_components": [c.to_dict() for c in components],
        "_data": {"status": "waiting_for_upload"}
    }


def _process_csv_data(rows: List[Dict[str, str]], fieldnames: List[str], missing_strategy: str = 'ask') -> Dict[str, Any]:
    """Internal helper to process CSV data rows and fieldnames."""
    missing_counts = {f: 0 for f in fieldnames}
    rows_with_missing = set()
    
    for i, row in enumerate(rows):
        for f in fieldnames:
            val = row.get(f, "").strip()
            if not val:
                missing_counts[f] += 1
                rows_with_missing.add(i)

    total_missing = sum(missing_counts.values())
    components = []
    
    if total_missing > 0:
        if missing_strategy == 'ask':
            components.append(Alert(message=f"Detected {total_missing} missing values across {len(rows_with_missing)} rows.", variant="warning", title="Missing Data Detected"))
            missing_stats_rows = [[f, str(count)] for f, count in missing_counts.items() if count > 0]
            components.append(Table(headers=["Column", "Missing Count"], rows=missing_stats_rows))
            components.append(Text(content="How would you like to handle the missing data? You can tell me to 'drop' the rows with missing data or 'fill' them with synthetic averages/defaults.", variant="body"))
            return {
                "_ui_components": [c.to_dict() for c in components],
                "_data": {"status": "waiting_for_missing_strategy", "missing_counts": missing_counts}
            }
        elif missing_strategy == 'drop':
            rows = [r for i, r in enumerate(rows) if i not in rows_with_missing]
            components.append(Alert(message=f"Dropped {len(rows_with_missing)} rows containing missing data.", variant="info"))
        elif missing_strategy == 'fill_synthetic':
            for f in fieldnames:
                if missing_counts[f] > 0:
                    numeric_vals = []
                    for r in rows:
                        v = r.get(f, "").strip()
                        if v:
                            try:
                                numeric_vals.append(float(v))
                            except ValueError:
                                pass
                    if len(numeric_vals) > 0 and len(numeric_vals) >= len(rows) / 2:
                        mean_val = sum(numeric_vals) / len(numeric_vals)
                        str_mean = f"{mean_val:.2f}"
                        for r in rows:
                            if not r.get(f, "").strip():
                                r[f] = str_mean
                    else:
                        for r in rows:
                            if not r.get(f, "").strip():
                                r[f] = "Unknown"
            components.append(Alert(message="Filled missing data with synthetic averages or defaults.", variant="success"))

    if not rows:
        return create_ui_response(components + [Alert(message="No data remaining after applying missing data strategy.", variant="error")])

    numeric_cols = []
    for f in fieldnames:
        is_num = True
        for r in rows:
            v = r.get(f, "").strip()
            if v:
                try:
                    float(v)
                except ValueError:
                    is_num = False
                    break
        if is_num:
            numeric_cols.append(f)

    metrics = []
    for f in numeric_cols[:4]:
        vals = [float(r[f]) for r in rows if r.get(f, "").strip()]
        if vals:
            avg = sum(vals) / len(vals)
            metrics.append(MetricCard(title=f"Avg {f}", value=f"{avg:.2f}", variant="default"))

    if metrics:
        components.append(Grid(columns=len(metrics), children=metrics, id="generic-metrics"))

    preview_rows = []
    for r in rows[:5]:
        preview_rows.append([str(r.get(f, "")) for f in fieldnames])
        
    components.append(Card(
        title=f"Data Analysis ({len(rows)} rows)",
        content=[Table(headers=fieldnames, rows=preview_rows)]
    ))

    return {
        "_ui_components": [c.to_dict() for c in components],
        "_data": {"processed_rows": len(rows), "columns": fieldnames, "stats_computed": True}
    }


def analyze_generic_data(csv_data: str, missing_strategy: str = 'ask', session_id: str = "default", **kwargs) -> Dict[str, Any]:
    """Analyze a generic CSV dataset.
    
    Args:
        csv_data: Raw CSV string data.
        missing_strategy: Strategy to handle missing data ('ask', 'drop', 'fill_synthetic').
    """
    # Strip markdown code fences if the LLM includes them
    csv_data = csv_data.strip()
    if csv_data.startswith("```csv"):
        csv_data = csv_data[6:].strip()
    elif csv_data.startswith("```"):
        csv_data = csv_data[3:].strip()
    if csv_data.endswith("```"):
        csv_data = csv_data[:-3].strip()

    try:
        reader = csv.DictReader(io.StringIO(csv_data))
        rows = list(reader)
        if not rows:
            return create_ui_response([Alert(message="CSV contains no data rows.", variant="error")])
        fieldnames = reader.fieldnames or []
        return _process_csv_data(rows, fieldnames, missing_strategy)
    except Exception as e:
        return create_ui_response([Alert(message=f"Failed to parse CSV: {e}", variant="error")])


def analyze_csv_file(
    attachment_id: Optional[str] = None,
    missing_strategy: str = 'ask',
    session_id: str = "default",
    user_id: Optional[str] = None,
    **kwargs
) -> Dict[str, Any]:
    """Analyze a CSV file the user uploaded via the chat composer.

    Args:
        attachment_id: AstralDeep attachment_id for the uploaded CSV.
        missing_strategy: Strategy for missing data ('ask', 'drop', 'fill_synthetic').

    The blob is located through the ownership-checked attachment resolver;
    the tool never accepts a caller-supplied on-disk path.
    """
    try:
        from agents.general.file_tools import resolve_attachment
    except ImportError as e:
        return create_ui_response([Alert(message=f"Attachments subsystem unavailable: {e}", variant="error")])

    _att, blob_path, err = resolve_attachment(attachment_id, user_id)
    if err is not None:
        return create_ui_response([Alert(message=err["error"]["message"], variant="error")])

    try:
        with open(blob_path, mode='r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            if not rows:
                return create_ui_response([Alert(message="CSV file contains no data rows.", variant="error")])
            fieldnames = reader.fieldnames or []
            return _process_csv_data(rows, fieldnames, missing_strategy)
    except Exception as e:
        return create_ui_response([Alert(message=f"Failed to read CSV file: {e}", variant="error")])


TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "search_patients": {
        "function": search_patients,
        "scope": "tools:read",
        "description": "Search patients by age range and/or condition. Returns a table of matching patients with status metrics.",
        "input_schema": {
            "type": "object",
            "properties": {
                "min_age": {"type": "integer", "description": "Minimum patient age", "default": 0},
                "max_age": {"type": "integer", "description": "Maximum patient age", "default": 200},
                "condition": {"type": "string", "description": "Condition keyword to filter by (partial match)", "default": ""}
            }
        }
    },
    "generate_synthetic_patients": {
        "function": generate_synthetic_patients,
        "scope": "tools:write",
        "description": "Generate mock synthetic patient data and provide a FileDownload UI component.",
        "input_schema": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "Number of records to generate", "default": 50}
            }
        }
    },
    "analyze_patient_data": {
        "function": analyze_patient_data,
        "scope": "tools:read",
        "description": "Request a patient dataset upload using the FileUpload UI component. Use this ONLY if the user hasn't provided data yet.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    "analyze_generic_data": {
        "function": analyze_generic_data,
        "scope": "tools:read",
        "description": "Analyze generic CSV data and compute statistics. MUST BE USED for ANY CSV data provided by the user, regardless of column names! Do NOT enforce strict patient data columns.",
        "input_schema": {
            "type": "object",
            "properties": {
                "csv_data": {"type": "string", "description": "Raw CSV string data"},
                "missing_strategy": {"type": "string", "description": "Strategy for missing data: 'ask', 'drop', 'fill_synthetic'. If not provided, it will stop and ask the user what to do.", "default": "ask"}
            },
            "required": ["csv_data"]
        }
    },
    "analyze_csv_file": {
        "function": analyze_csv_file,
        "scope": "tools:read",
        "description": "Analyze a CSV file the user uploaded via the chat composer. USE THIS for LARGE CSV files that are already uploaded. Provide the attachment_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "attachment_id": {"type": "string", "description": "AstralDeep attachment_id from the chat composer"},
                "missing_strategy": {"type": "string", "description": "Strategy for missing data: 'ask', 'drop', 'fill_synthetic'.", "default": "ask"}
            },
            "required": ["attachment_id"]
        }
    }
}
