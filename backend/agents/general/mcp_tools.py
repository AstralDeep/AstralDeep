"""
MCP Tools — tool functions that return UI Primitives.

Includes:
- Patient tools (mock): search_patients, graph_patient_data
- System tools: get_system_status, get_cpu_info, get_memory_info, get_disk_info
- System streaming tools: live_system_metrics (push streaming, 001-tool-stream-ui)
- Search tools: search_wikipedia
"""
import asyncio
import os
import sys
from typing import List, Dict, Any, AsyncIterator
import arxiv
from openai import OpenAI
from typing import Optional
from collections import Counter
import json
import csv
import io
import time
import logging

logger = logging.getLogger(__name__)

# Data processing dependencies (optional)
try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    pd = None
    PANDAS_AVAILABLE = False

# Expression evaluator
from shared.expression_evaluator import ExpressionEvaluator  # noqa: E402
from shared.llm_text import strip_reasoning_markup  # noqa: E402


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from astralprims import (  # noqa: E402
    Text, Card, Table, Container, MetricCard, Alert, Grid, PlotlyChart, List_,
    FileDownload, create_ui_response, ColorPicker, Button, Divider,
    ThemeApply
)
from shared.stream_sdk import streaming_tool, StreamComponents  # noqa: E402


def generate_dynamic_chart(
    data: List[Dict[str, Any]] | str, 
    x_key: str, 
    y_key: Optional[str] = None, 
    chart_type: str = "auto",
    title: str = "Data Visualization",
    session_id: str = "default",
    **kwargs
) -> Dict[str, Any]:
    """Generate a chart dynamically based on generic input data.

    Args:
        data: List of dictionaries representing the dataset (e.g., [{"date": "2023", "sales": 10}, ...]).
        x_key: The dictionary key to use for the X-axis (labels/categories).
        y_key: The dictionary key for the Y-axis. If None, it plots the frequency count of x_key.
        chart_type: 'auto', 'bar', 'line', 'pie', or 'scatter' (default: 'auto').
        title: Title of the chart.

    Returns:
        Dict with _ui_components and _data keys.
    """
    
    # 1. Parse stringified JSON if necessary
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return {
                "_ui_components": [Alert(message="Invalid data format provided. Expected JSON array.", variant="error").to_dict()],
                "_data": {}
            }

    if not data:
        return {
            "_ui_components": [Alert(message="No data provided to graph.", variant="warning").to_dict()],
            "_data": {}
        }

    # --- THE FIX ---
    # Normalize y_key (LLMs sometimes pass the string "null", "None", or "")
    if str(y_key).strip().lower() in ("null", "none", "", "undefined"):
        y_key = None

    # 2. Extract Data Safely
    labels = []
    values = []

    if y_key is None:
        # Frequency count mode (Now triggered correctly!)
        raw_labels = [str(row.get(x_key, "Unknown")) for row in data]
        counts = Counter(raw_labels)
        labels = list(counts.keys())
        values = list(counts.values())
        y_key = "count" 
    else:
        # Explicit X vs Y mode
        labels = [str(row.get(x_key, "")) for row in data]
        
        for row in data:
            try:
                values.append(float(row.get(y_key, 0)))
            except (ValueError, TypeError):
                values.append(0)

    # 3. Auto-determine Chart Type
    if chart_type == "auto":
        unique_labels = len(set(labels))
        is_date_like = len(labels) > 0 and any(char in labels[0] for char in ['-', '/']) and any(char.isdigit() for char in labels[0])
        
        if is_date_like:
            chart_type = "line" 
        elif y_key == "count" and unique_labels <= 10:
            chart_type = "pie"  
        else:
            chart_type = "bar" 

    # 4. Build Plotly Configuration
    chart_data = []
    layout_update = {}
    color_palette = ["#6366F1", "#8B5CF6", "#06B6D4", "#10B981", "#F59E0B", "#EF4444", "#EC4899", "#3B82F6"]

    if chart_type == "pie":
        chart_data = [{
            "labels": labels,
            "values": values,
            "type": "pie",
            "marker": {"colors": color_palette},
            "textinfo": "label+percent",
            "hole": 0.4 
        }]
    else:
        trace = {
            "x": labels,
            "y": values,
            "type": "bar" if chart_type == "bar" else "scatter",
            "marker": {"color": color_palette[0]}
        }
        
        if chart_type == "line":
            trace["mode"] = "lines+markers"
            trace["line"] = {"color": color_palette[0], "width": 3}
        elif chart_type == "scatter":
            trace["mode"] = "markers"
            
        chart_data.append(trace)

        # Plotly layout fixes for categories
        if chart_type == "bar":
            layout_update["xaxis"] = {
                "type": "category",
                "tickangle": -45,
                "categoryorder": "category ascending" # This forces the X-axis to sort neatly!
            }

    # 5. Build UI Primitives
    chart = PlotlyChart(
        title=title,
        data=chart_data,
        layout=layout_update,
        id="dynamic-chart"
    )

    components = [
        Card(
            title=title,
            id="chart-card",
            content=[chart]
        )
    ]

    return {
        "_ui_components": [c.to_dict() for c in components],
        "_data": {
            "labels": labels, 
            "values": values, 
            "x_key": x_key, 
            "y_key": y_key, 
            "rendered_chart_type": chart_type
        }
    }



def _confine_input_path(file_path: str, user_id: str) -> Optional[str]:
    """Return the real path of ``file_path`` if this user may read it, else None.

    Only the caller's own attachment upload root and their own download
    directory qualify. Tool arguments are model-supplied, so an
    unconstrained absolute path would read any file this process can open.
    """
    # A falsy user_id would collapse the per-user roots to ``backend/tmp`` and
    # ``<upload_root>`` themselves — matching EVERY user's subtree — so refuse
    # it outright rather than confine against a tenant-wide root.
    if not user_id or not str(user_id).strip():
        return None
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    roots = [os.path.join(backend_dir, "tmp", str(user_id))]
    try:
        from orchestrator.attachments import store
        roots.append(os.path.join(str(store.get_upload_root()), str(user_id)))
    except ImportError:
        pass

    real = os.path.realpath(file_path)
    for root in roots:
        real_root = os.path.realpath(root)
        if real == real_root or real.startswith(real_root + os.sep):
            return real
    return None


def modify_data(
    modifications: Optional[List[Dict[str, Any]]] = None,
    csv_data: Optional[str] = None,
    filename: Optional[str] = None,
    file_path: Optional[str] = None,
    file_handle: Optional[str] = None,
    output_format: Optional[str] = None,
    session_id: str = "default",
    user_id: str = "legacy",
    **kwargs
) -> Dict[str, Any]:
    """Apply modifications to a CSV/Excel dataset and provide a download link.

    Supports row-based calculations, conditional logic, and Excel file formats.

    Args:
        csv_data: Raw CSV string data (optional if file_path/file_handle is provided).
        file_handle: AstralDeep attachment_id for a CSV/Excel uploaded via the
            chat composer. Resolved to a real on-disk path server-side; preferred
            over file_path for chat-uploaded files.
        modifications: List of modifications to apply. Each modification can have:
            - action: "add_column", "update_column", "calculate_column"
            - name: Column name
            - value: Static value (optional if expression provided)
            - expression: Python-like expression using row["column"] (optional)
            - default: Fallback value if expression fails (optional)
            - overwrite: Whether to overwrite existing column (default True)
            - dtype: Data type for conversion ("string", "integer", "float", "boolean")
        filename: Optional filename for the modified file (default: modified_data_<timestamp>.<ext>).
        file_path: Server-side path to a CSV/Excel file. Not model-facing —
            it is confined to the caller's own upload/download directories.
        output_format: Output file format ("csv" or "excel"). Defaults to input format.
    """
    if modifications is None:
        modifications = []

    if file_handle:
        try:
            from shared.attachment_resolver import resolve_attachment_path
            file_path = resolve_attachment_path(file_handle, user_id)
        except ValueError as e:
            return create_ui_response([Alert(message=str(e), variant="error")])
    elif file_path:
        confined = _confine_input_path(file_path, user_id)
        if confined is None:
            return create_ui_response([Alert(
                message=(
                    "That file is not readable here. Upload it in the chat "
                    "composer and pass the returned attachment_id as 'file_handle'."
                ),
                variant="error",
            )])
        file_path = confined

    try:
        # Determine input format
        input_format = None
        if file_path:
            if not os.path.exists(file_path):
                return create_ui_response([Alert(message=f"File not found: {file_path}", variant="error")])
            # Detect format from extension
            if file_path.lower().endswith('.csv'):
                input_format = 'csv'
            elif file_path.lower().endswith(('.xlsx', '.xls')):
                input_format = 'excel'
            else:
                # Default to CSV
                input_format = 'csv'
        else:
            # No file path, assume CSV data
            input_format = 'csv'

        # Load data
        rows = []
        fieldnames = []
        df = None
        
        if PANDAS_AVAILABLE and input_format == 'excel':
            # Use pandas for Excel
            df = pd.read_excel(file_path)
            rows = df.to_dict('records')
            fieldnames = list(df.columns)
        else:
            # CSV fallback (or pandas not available)
            if file_path:
                with open(file_path, mode='r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    fieldnames = list(reader.fieldnames) if reader.fieldnames else []
                    rows = list(reader)
            elif csv_data:
                # Strip markdown code fences
                csv_data = csv_data.strip()
                if csv_data.startswith("```csv"):
                    csv_data = csv_data[6:].strip()
                elif csv_data.startswith("```"):
                    csv_data = csv_data[3:].strip()
                if csv_data.endswith("```"):
                    csv_data = csv_data[:-3].strip()

                reader = csv.DictReader(io.StringIO(csv_data))
                fieldnames = list(reader.fieldnames) if reader.fieldnames else []
                rows = list(reader)
            else:
                return create_ui_response([Alert(message="Neither csv_data nor file_path provided.", variant="error")])

        # Process each modification
        for mod in modifications:
            action = mod.get("action")
            name = mod.get("name", "")
            value = mod.get("value")
            expression = mod.get("expression")
            default = mod.get("default")
            overwrite = mod.get("overwrite", True)
            dtype = mod.get("dtype")

            if action == "drop_column":
                if name in fieldnames:
                    fieldnames.remove(name)
                    for row in rows:
                        row.pop(name, None)
                    if df is not None and PANDAS_AVAILABLE:
                        df = df.drop(columns=[name])
                continue
                
            elif action == "rename_column":
                new_name = value
                if name in fieldnames and new_name:
                    idx = fieldnames.index(name)
                    fieldnames[idx] = new_name
                    for row in rows:
                        if name in row:
                            row[new_name] = row.pop(name)
                    if df is not None and PANDAS_AVAILABLE:
                        df = df.rename(columns={name: new_name})
                continue
                
            elif action == "filter_rows":
                if expression:
                    evaluator = None
                    try:
                        evaluator = ExpressionEvaluator(expression)
                    except Exception as e:
                        return create_ui_response([Alert(message=f"Invalid expression '{expression}': {e}", variant="error")])
                    
                    filtered_rows = []
                    for row in rows:
                        try:
                            if evaluator.evaluate(row):
                                filtered_rows.append(row)
                        except Exception:
                            # if error evaluating, keep or drop? let's drop if evaluate fails unless default is True
                            if str(default).lower() == 'true':
                                filtered_rows.append(row)
                    rows = filtered_rows
                    if df is not None and PANDAS_AVAILABLE:
                        df = pd.DataFrame(rows)
                continue
                
            elif action == "sort_rows":
                 reverse = str(value).lower() == 'desc'
                 rows.sort(key=lambda r: (r.get(name) is None, r.get(name, "")), reverse=reverse)
                 if df is not None and PANDAS_AVAILABLE:
                     df = pd.DataFrame(rows)
                 continue

            # Determine if column exists
            column_exists = name in fieldnames
            
            # Add column to fieldnames if needed
            if action in ("add_column", "calculate_column") and not column_exists:
                fieldnames.append(name)
            elif action == "update_column" and not column_exists:
                # update_column on non-existing column is treated as add_column
                fieldnames.append(name)
                column_exists = True
            
            # Prepare evaluator if expression provided
            evaluator = None
            if expression:
                try:
                    evaluator = ExpressionEvaluator(expression)
                except Exception as e:
                    return create_ui_response([
                        Alert(message=f"Invalid expression '{expression}': {e}", variant="error")
                    ])
            
            # Apply modification row by row
            for i, row in enumerate(rows):
                result = None
                if expression and evaluator:
                    try:
                        result = evaluator.evaluate(row)
                    except Exception:
                        result = default if default is not None else value
                else:
                    result = value
                
                # Apply data type conversion
                if dtype and result is not None:
                    try:
                        if dtype == "integer":
                            result = int(float(result))
                        elif dtype == "float":
                            result = float(result)
                        elif dtype == "boolean":
                            result = bool(result)
                        elif dtype == "string":
                            result = str(result)
                    except (ValueError, TypeError):
                        pass  # Keep original result
                
                # Store result
                if overwrite or not column_exists or action == "add_column":
                    row[name] = result
                elif action == "update_column" and column_exists:
                    row[name] = result
                # calculate_column always overwrites if overwrite=True (default)
                
            # Update DataFrame if using pandas (for vectorized operations)
            if df is not None and name in fieldnames and PANDAS_AVAILABLE:
                # Reconstruct column from rows (simpler but less efficient)
                # In future could use pandas vectorized evaluation
                df[name] = [row.get(name) for row in rows]

        # Determine output format
        if output_format is None:
            output_format = input_format  # Default to same as input
        
        if output_format not in ("csv", "excel"):
            output_format = "csv"
        
        # Generate filename
        timestamp = int(time.time())
        if not filename:
            ext = "csv" if output_format == "csv" else "xlsx"
            if file_path:
                basename = os.path.basename(file_path)
                name_without_ext = os.path.splitext(basename)[0]
                filename = f"{name_without_ext}_modified.{ext}"
            else:
                filename = f"modified_data_{timestamp}.{ext}"
        
        backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
        download_dir = os.path.join(backend_dir, "tmp", user_id, session_id)
        os.makedirs(download_dir, exist_ok=True)
        out_file_path = os.path.join(download_dir, filename)

        # Save output
        if PANDAS_AVAILABLE and output_format == "excel" and df is not None:
            # Use pandas to write Excel
            df.to_excel(out_file_path, index=False)
        else:
            # Write CSV (fallback)
            with open(out_file_path, mode='w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)

        # Root-relative download URL — resolved against the serving origin by
        # the browser (Constitution X: no hard-coded localhost; feature 030).
        download_url = f"/api/download/{session_id}/{filename}"

        # Prepare preview (first 5 rows, first 5 columns)
        preview_headers = fieldnames[:5]
        preview_rows = [[str(row.get(f, "")) for f in preview_headers] for row in rows[:5]]

        components = [
            Card(
                title="Data Modified Successfully",
                id="modify-data-card",
                content=[
                    Alert(
                        message=f"Applied {len(modifications)} modifications to {len(rows)} rows. Output format: {output_format.upper()}.",
                        variant="success"
                    ),
                    FileDownload(
                        label=f"Download {filename}",
                        url=download_url,
                        filename=filename
                    ),
                    Table(
                        headers=preview_headers,
                        rows=preview_rows,
                        id="modify-data-preview"
                    )
                ]
            )
        ]

        return {
            "_ui_components": [c.to_dict() for c in components],
            "_data": {
                "filename": filename,
                "file_path": out_file_path,
                "rows_count": len(rows),
                "output_format": output_format,
                "modifications_applied": len(modifications)
            }
        }

    except Exception as e:
        return create_ui_response([Alert(message=f"Failed to modify data: {e}", variant="error")])


# =============================================================================
# SYSTEM TOOLS
# =============================================================================
import psutil  # noqa: E402
import platform  # noqa: E402

# If running inside Docker with host procfs/sysfs mounted, point psutil at the host
_host_proc = os.environ.get("HOST_PROC")
if _host_proc and os.path.isdir(_host_proc):
    os.environ["PSUTIL_PROCFS_PATH"] = _host_proc  # psutil >= 6.x
    psutil.PROCFS_PATH = _host_proc                 # direct override for older psutil

# Root path for disk usage — use host rootfs if mounted, otherwise container root
_disk_root = "/hostfs" if os.path.isdir("/hostfs") else "/"


def get_system_status(session_id: str = "default", **kwargs) -> Dict[str, Any]:
    """Get comprehensive system status information."""
    cpu_percent = psutil.cpu_percent(interval=0)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(_disk_root)

    def get_variant(percent):
        if percent > 90:
            return "error"
        if percent > 70:
            return "warning"
        return "default"

    components = [
        Card(
            title="System Status",
            id="system-status-card",
            content=[
                Grid(
                    columns=3,
                    children=[
                        MetricCard(
                            title="CPU Usage",
                            value=f"{cpu_percent}%",
                            progress=cpu_percent / 100,
                            variant=get_variant(cpu_percent),
                            id="cpu-metric"
                        ),
                        MetricCard(
                            title="Memory Usage",
                            value=f"{mem.percent}%",
                            subtitle=f"{mem.used // (1024**3):.1f} / {mem.total // (1024**3):.1f} GB",
                            progress=mem.percent / 100,
                            variant=get_variant(mem.percent),
                            id="mem-metric"
                        ),
                        MetricCard(
                            title="Disk Usage",
                            value=f"{disk.percent}%",
                            subtitle=f"{disk.used // (1024**3):.1f} / {disk.total // (1024**3):.1f} GB",
                            progress=disk.percent / 100,
                            variant=get_variant(disk.percent),
                            id="disk-metric"
                        ),
                    ]
                ),
                Text(content=f"Platform: {platform.system()} {platform.release()}", variant="caption"),
                Text(content=f"Hostname: {platform.node()}", variant="caption"),
            ]
        )
    ]

    return {
        "_ui_components": [c.to_dict() for c in components],
        "_data": {
            "cpu_percent": cpu_percent,
            "memory_percent": mem.percent,
            "disk_percent": disk.percent,
            "platform": platform.system(),
        }
    }


def get_cpu_info(session_id: str = "default", **kwargs) -> Dict[str, Any]:
    """Get detailed CPU information."""
    cpu_freq = psutil.cpu_freq()
    cpu_count = psutil.cpu_count()
    cpu_percent_per_core = psutil.cpu_percent(interval=0, percpu=True)

    headers = ["Core", "Usage %"]
    rows = [[f"Core {i}", f"{p}%"] for i, p in enumerate(cpu_percent_per_core)]

    components = [
        Card(
            title="CPU Information",
            id="cpu-info-card",
            content=[
                Grid(columns=2, children=[
                    MetricCard(title="Total Cores", value=str(cpu_count), id="cpu-cores"),
                    MetricCard(
                        title="Frequency",
                        value=f"{cpu_freq.current:.0f} MHz" if cpu_freq else "N/A",
                        id="cpu-freq"
                    ),
                ]),
                Table(headers=headers, rows=rows, id="cpu-table"),
            ]
        )
    ]

    return {
        "_ui_components": [c.to_dict() for c in components],
        "_data": {"cores": cpu_count, "frequency": cpu_freq.current if cpu_freq else 0}
    }


def get_memory_info(session_id: str = "default", **kwargs) -> Dict[str, Any]:
    """Get detailed memory information."""
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()

    components = [
        Card(
            title="Memory Information",
            id="memory-info-card",
            content=[
                Grid(columns=2, children=[
                    MetricCard(
                        title="RAM Usage",
                        value=f"{mem.percent}%",
                        subtitle=f"{mem.used // (1024**3):.1f} / {mem.total // (1024**3):.1f} GB",
                        progress=mem.percent / 100,
                        id="ram-metric"
                    ),
                    MetricCard(
                        title="Swap Usage",
                        value=f"{swap.percent}%",
                        subtitle=f"{swap.used // (1024**3):.1f} / {swap.total // (1024**3):.1f} GB",
                        progress=swap.percent / 100,
                        id="swap-metric"
                    ),
                ]),
            ]
        )
    ]

    return {
        "_ui_components": [c.to_dict() for c in components],
        "_data": {"ram_percent": mem.percent, "swap_percent": swap.percent}
    }


def get_disk_info(session_id: str = "default", **kwargs) -> Dict[str, Any]:
    """Get disk partition information."""
    partitions = psutil.disk_partitions()
    headers = ["Device", "Mount", "FS Type", "Total GB", "Used GB", "Free GB", "Usage %"]
    rows = []

    for p in partitions:
        try:
            usage = psutil.disk_usage(p.mountpoint)
            rows.append([
                p.device,
                p.mountpoint,
                p.fstype,
                f"{usage.total // (1024**3):.1f}",
                f"{usage.used // (1024**3):.1f}",
                f"{usage.free // (1024**3):.1f}",
                f"{usage.percent}%"
            ])
        except PermissionError:
            rows.append([p.device, p.mountpoint, p.fstype, "N/A", "N/A", "N/A", "N/A"])

    components = [
        Card(
            title="Disk Information",
            id="disk-info-card",
            content=[
                Table(headers=headers, rows=rows, id="disk-table"),
            ]
        )
    ]

    return {
        "_ui_components": [c.to_dict() for c in components],
        "_data": {"partitions": len(rows)}
    }


# =============================================================================
# STREAMING SYSTEM TOOLS (001-tool-stream-ui)
# =============================================================================
#
# `live_system_metrics` is the push-streaming counterpart to the four
# legacy poll-based system tools above. Where those return a single snapshot
# (and rely on the orchestrator's polling loop in FF_LIVE_STREAMING to call
# them repeatedly), this one is an async generator that pushes a fresh
# snapshot every `interval_s` seconds via the new push pipeline. The agent
# owns the cadence, so we can:
#
# - Run psutil.cpu_percent() with the proper sampling interval (which the
#   poll path can't do — it always passes interval=0).
# - Coalesce all three metrics into ONE Card with a single stream id, so
#   the user sees one component updating in place rather than three
#   independently-rendering cards racing each other.
# - Clean up cleanly via the try/finally pattern when the user navigates
#   away (the orchestrator sends ToolStreamCancel which propagates as
#   GeneratorExit through this function).
#
# Existing one-shot calls to get_system_status / get_cpu_info / get_memory_info /
# get_disk_info are unchanged. The legacy poll path still works for users
# who haven't enabled FF_TOOL_STREAMING.

@streaming_tool(
    name="live_system_metrics",
    description=(
        "Stream live CPU, memory, and disk usage metrics. Pushes a fresh "
        "system status card every interval_s seconds, updating the same "
        "component in place. Use this for monitoring dashboards instead of "
        "the snapshot-style get_system_status."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "interval_s": {
                "type": "integer",
                "minimum": 1,
                "maximum": 30,
                "default": 2,
                "description": "How often to emit a fresh sample (seconds, 1-30)",
            },
        },
    },
    max_fps=2,   # cap at 2 fps — system metrics never need more than that
    min_fps=1,
)
async def live_system_metrics(
    args: Dict[str, Any], credentials: Dict[str, Any],
) -> AsyncIterator[StreamComponents]:
    """Push CPU + memory + disk usage as a single Card every interval_s seconds.

    Why a single Card with a Grid of three MetricCards rather than three
    top-level streams: the orchestrator merges-by-id at the top level, and
    one consolidated component means one stream subscription, one network
    chunk per update, one render. The frontend renders three pulse-updating
    metrics inside a stable parent — exactly the htop / Activity Monitor
    pattern users expect.

    Cleanup: the try/finally swallows GeneratorExit when the user leaves
    the chat. psutil holds no per-process state we need to release here,
    but the pattern is required by the SDK contract for any future tool
    that does (e.g. an open file handle, a socket).
    """
    interval = max(1, min(30, int(args.get("interval_s", 2))))

    def _variant(percent: float) -> str:
        if percent > 90:
            return "error"
        if percent > 70:
            return "warning"
        return "default"

    # Prime the CPU sampler with a non-blocking 0-interval call so that the
    # FIRST yield reflects activity since startup rather than a misleading
    # 0%. (psutil.cpu_percent returns the delta since the previous call.)
    psutil.cpu_percent(interval=0)

    try:
        while True:
            # Sample. cpu_percent with interval=None returns the delta since
            # the previous call — we control the sampling window via the
            # asyncio.sleep below, so passing interval=0 is correct here.
            cpu_percent = psutil.cpu_percent(interval=0)
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage(_disk_root)

            card = Card(
                title="Live System Metrics",
                content=[
                    Grid(
                        columns=3,
                        children=[
                            MetricCard(
                                title="CPU Usage",
                                value=f"{cpu_percent:.1f}%",
                                progress=cpu_percent / 100,
                                variant=_variant(cpu_percent),
                            ),
                            MetricCard(
                                title="Memory Usage",
                                value=f"{mem.percent:.1f}%",
                                subtitle=f"{mem.used / (1024**3):.1f} / {mem.total / (1024**3):.1f} GB",
                                progress=mem.percent / 100,
                                variant=_variant(mem.percent),
                            ),
                            MetricCard(
                                title="Disk Usage",
                                value=f"{disk.percent:.1f}%",
                                subtitle=f"{disk.used / (1024**3):.1f} / {disk.total / (1024**3):.1f} GB",
                                progress=disk.percent / 100,
                                variant=_variant(disk.percent),
                            ),
                        ],
                    ),
                    Text(
                        content=f"Sampled every {interval}s · Platform: {platform.system()} {platform.release()}",
                        variant="caption",
                    ),
                ],
            )

            yield StreamComponents(
                components=[card.to_dict()],
                raw={
                    "cpu_percent": cpu_percent,
                    "memory_percent": mem.percent,
                    "memory_used_gb": mem.used / (1024**3),
                    "memory_total_gb": mem.total / (1024**3),
                    "disk_percent": disk.percent,
                    "disk_used_gb": disk.used / (1024**3),
                    "disk_total_gb": disk.total / (1024**3),
                    "ts": time.time(),
                },
            )

            await asyncio.sleep(interval)
    finally:
        logger.info("live_system_metrics stream stopping")


# =============================================================================
# SEARCH TOOLS
# =============================================================================
import requests  # noqa: E402


def search_wikipedia(query: str, language: str = "en", session_id: str = "default", **kwargs) -> Dict[str, Any]:
    """Search Wikipedia for articles and summaries.

    Args:
        query: The search query
        language: Wikipedia language code (default: 'en')

    Returns:
        Dict with _ui_components and _data keys.
    """
    try:
        search_url = f"https://{language}.wikipedia.org/w/api.php"
        params = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "format": "json",
            "srlimit": 5,
            "srprop": "snippet|titlesnippet"
        }
        headers = {
            "User-Agent": "AstralDeep/1.0 (internal research project)"
        }
        resp = requests.get(search_url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("query", {}).get("search", [])

        if not results:
            return create_ui_response([
                Alert(message=f"No Wikipedia results for '{query}'", variant="info")
            ])

        items = []
        for r in results:
            # Clean HTML from snippet
            snippet = r.get("snippet", "").replace("<span class=\"searchmatch\">", "").replace("</span>", "")
            items.append(f"**{r['title']}** — {snippet}")

        components = [
            Card(
                title=f"Wikipedia: {query}",
                id="wiki-results",
                content=[
                    List_(items=items, id="wiki-list"),
                ]
            )
        ]

        return {
            "_ui_components": [c.to_dict() for c in components],
            "_data": {"results": [{"title": r["title"], "pageid": r["pageid"]} for r in results]}
        }

    except Exception as e:
        return create_ui_response([
            Alert(message=f"Wikipedia search failed: {str(e)}", variant="error", title="Error")
        ])


# =============================================================================
# ACADEMIC SEARCH TOOLS
# =============================================================================

def extract_search_terms(query: str, **kwargs) -> str:
    """Extract relevant search terms from a natural language query using LLM."""
    logger.debug(f"Extracting search terms for: {query}")
    # Feature 054: the per-turn credentials the orchestrator injects
    # (_session_llm_credentials) are preferred, then the agent's own
    # credential bundle. No env fallback — the operator-default path is gone.
    session_llm = kwargs.get("_session_llm_credentials") or {}
    creds = kwargs.get("_credentials", {}) or {}
    api_key = (
        session_llm.get("OPENAI_API_KEY")
        or (creds.get("OPENAI_API_KEY") if not kwargs.get("_credentials_encrypted") else None)
    )
    base_url = (
        session_llm.get("OPENAI_BASE_URL")
        or (creds.get("OPENAI_BASE_URL") if not kwargs.get("_credentials_encrypted") else None)
    )
    model = (
        session_llm.get("LLM_MODEL")
        or "gpt-4o"
    )
    
    if not api_key:
        logger.warning("OPENAI_API_KEY not found, using raw query")
        return query.strip()
        
    try:
        client = OpenAI(api_key=api_key, base_url=base_url)
        logger.debug("Calling LLM for search terms...")
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a search query optimizer. Extract the core subject/keywords from the user's request for an academic paper search. Return ONLY the keywords, nothing else. Example: 'write a paper about deep learning' -> 'deep learning'"},
                {"role": "user", "content": query}
            ],
            max_tokens=50,
            timeout=10 # Add timeout
        )
        terms = strip_reasoning_markup(response.choices[0].message.content or "").strip()
        logger.debug(f"extracted terms: {terms}")
        return terms or query.strip()
    except Exception as e:
        logger.error(f"Error extracting search terms: {e}")
        return query.strip()

def search_arxiv(query: str, max_results: int = 10, session_id: str = "default", **kwargs) -> Dict[str, Any]:
    """Search arXiv for papers related to the query.
    
    Args:
        query: The search query
        max_results: Maximum number of results (default: 10)
    """
    logger.debug(f"search_arxiv called with query: {query}")
    # Use LLM to extract clean search terms
    clean_query = extract_search_terms(query, **kwargs)
    logger.debug(f"Original query: '{query}' -> Cleaned query: '{clean_query}'")
    
    try:
        logger.debug("Executing arxiv search...")
        search = arxiv.Search(
            query=clean_query,
            max_results=int(max_results),
            sort_by=arxiv.SortCriterion.Relevance
        )
        
        results = []
        for paper in search.results():
            results.append({
                "title": paper.title,
                "authors": [author.name for author in paper.authors],
                "summary": paper.summary,
                "published": paper.published.strftime("%Y-%m-%d"),
                "url": paper.entry_id,
                "pdf_url": paper.pdf_url
            })
        logger.debug(f"Found {len(results)} papers")
        
        # Create UI components
        if not results:
            components = [
                Alert(
                    title="No Results",
                    message=f"No papers found for '{query}' on arXiv.",
                    variant="warning"
                )
            ]
        else:
            # Calculate metrics
            total_papers = len(results)
            latest_date = max(r["published"] for r in results) if results else "N/A"
            # Find most common author
            all_authors = [a for r in results for a in r["authors"]]
            top_author = max(set(all_authors), key=all_authors.count) if all_authors else "N/A"

            # Create list items with paper details
            list_items = []
            for paper in results:
                authors_str = ", ".join(paper["authors"][:2])
                if len(paper["authors"]) > 2:
                    authors_str += f" +{len(paper['authors'])-2}"
                
                list_items.append({
                    "title": paper["title"],
                    "subtitle": f"{authors_str} • {paper['published']}",
                    "description": paper["summary"][:200] + "..." if len(paper['summary']) > 200 else paper['summary'],
                    "url": paper["url"]
                })
            
            # Cohesive UI: Card with Metrics + List
            components = [
                Card(
                    title=f"ArXiv Research: {clean_query}",
                    id="arxiv-results-card",
                    content=[
                        Grid(
                            columns=3,
                            children=[
                                MetricCard(
                                    title="Total Papers",
                                    value=str(total_papers),
                                    id="metric-total"
                                ),
                                MetricCard(
                                    title="Latest Paper",
                                    value=latest_date,
                                    id="metric-date"
                                ),
                                MetricCard(
                                    title="Top Author",
                                    value=top_author,
                                    id="metric-author"
                                ),
                            ]
                        ),
                        Text(content="Most relevant papers found:", variant="body"),
                        List_(
                            items=list_items,
                            variant="detailed",
                            id="arxiv-list"
                        )
                    ]
                )
            ]
        
        return {
            "_ui_components": [c.to_dict() if hasattr(c, 'to_json') else c for c in components],
            "_data": results
        }
    except Exception as e:
        logger.error(f"Error searching arXiv: {e}")
        components = [
            Alert(
                title="Search Error",
                message=f"Error searching arXiv: {str(e)}",
                variant="error"
            )
        ]
        return create_ui_response(components)


# =============================================================================
# THEME CUSTOMIZATION
# =============================================================================

THEME_PRESETS = {
    "midnight": {
        "bg": "#0F1221", "surface": "#1A1E2E", "primary": "#6366F1",
        "secondary": "#8B5CF6", "text": "#F3F4F6", "muted": "#9CA3AF", "accent": "#06B6D4",
    },
    "daylight": {
        "bg": "#F8FAFC", "surface": "#FFFFFF", "primary": "#4F46E5",
        "secondary": "#7C3AED", "text": "#1E293B", "muted": "#64748B", "accent": "#0891B2",
    },
    "ocean": {
        "bg": "#0C1222", "surface": "#132038", "primary": "#0EA5E9",
        "secondary": "#06B6D4", "text": "#E2E8F0", "muted": "#94A3B8", "accent": "#2DD4BF",
    },
    "sunset": {
        "bg": "#1C1017", "surface": "#2D1B24", "primary": "#F97316",
        "secondary": "#EF4444", "text": "#FEF2F2", "muted": "#A8A29E", "accent": "#FBBF24",
    },
    "forest": {
        "bg": "#0F1A14", "surface": "#1A2E22", "primary": "#22C55E",
        "secondary": "#10B981", "text": "#ECFDF5", "muted": "#86EFAC", "accent": "#A3E635",
    },
}

THEME_COLOR_LABELS = {
    "bg": "Background",
    "surface": "Surface",
    "primary": "Primary",
    "secondary": "Secondary",
    "text": "Text",
    "muted": "Muted Text",
    "accent": "Accent",
}


def _build_theme_customization_card(active_preset: str = None):
    """Build the interactive theme customization Card component."""
    preset_buttons = []
    for name, colors in THEME_PRESETS.items():
        preset_buttons.append(
            Button(
                label=name.title(),
                action="theme_preset",
                payload={"name": name, "colors": colors},
                variant="secondary",
                id=f"theme-preset-{name}",
            )
        )

    default_colors = THEME_PRESETS.get(active_preset, THEME_PRESETS["midnight"])
    color_pickers = []
    for key, label in THEME_COLOR_LABELS.items():
        color_pickers.append(
            ColorPicker(
                label=label,
                color_key=key,
                value=default_colors[key],
                id=f"theme-color-{key}",
            )
        )

    return Card(
        title="Theme Customization",
        id="theme-card",
        content=[
            Text(content="Choose a preset theme:", variant="body"),
            Grid(
                columns=5,
                children=preset_buttons,
                gap=8,
            ),
            Divider(),
            Text(content="Or customize individual colors:", variant="body"),
            Container(children=color_pickers),
        ],
    )


def change_theme(preset: str = None, **kwargs) -> Dict[str, Any]:
    """Show theme customization interface with presets and color pickers."""
    components = [_build_theme_customization_card(preset)]

    return {
        "_ui_components": [c.to_dict() for c in components],
        "_data": {
            "message": "Theme customization panel rendered. Use the preset buttons or color pickers to change colors.",
            "presets": list(THEME_PRESETS.keys()),
        },
    }


import re  # noqa: E402

def apply_theme_preset(preset: str, **kwargs) -> Dict[str, Any]:
    """Apply a predefined theme preset directly."""
    preset = preset.lower().strip()
    if preset not in THEME_PRESETS:
        components = [
            Alert(
                title="Invalid Preset",
                message=f"Unknown theme preset '{preset}'. Available: {', '.join(THEME_PRESETS.keys())}",
                variant="error"
            )
        ]
        return create_ui_response(components)

    colors = THEME_PRESETS[preset]
    message = f"Theme changed to {preset.title()}"
    components = [
        ThemeApply(
            preset=preset,
            message=message,
            id="theme-apply-preset",
        ),
        _build_theme_customization_card(preset),
    ]
    return {
        "_ui_components": [c.to_dict() for c in components],
        "_data": {"applied_preset": preset, "colors": colors, "message": message},
    }


def set_theme_color(color_key: str, hex_value: str, **kwargs) -> Dict[str, Any]:
    """Change a single theme color."""
    color_key = color_key.lower().strip()
    hex_value = hex_value.strip()

    if color_key not in THEME_COLOR_LABELS:
        components = [
            Alert(
                title="Invalid Color Key",
                message=f"Unknown color key '{color_key}'. Valid keys: {', '.join(THEME_COLOR_LABELS.keys())}",
                variant="error"
            )
        ]
        return create_ui_response(components)

    if not re.match(r'^#[0-9a-fA-F]{6}$', hex_value):
        components = [
            Alert(
                title="Invalid Color",
                message=f"'{hex_value}' is not a valid hex color. Use format like '#FF5500'.",
                variant="error"
            )
        ]
        return create_ui_response(components)

    label = THEME_COLOR_LABELS[color_key]
    message = f"{label} color changed to {hex_value}"
    components = [
        ThemeApply(
            color_key=color_key,
            color_value=hex_value,
            message=message,
            id="theme-apply-color",
        ),
        _build_theme_customization_card(),
    ]
    return {
        "_ui_components": [c.to_dict() for c in components],
        "_data": {"color_key": color_key, "color_value": hex_value, "message": message},
    }


# =============================================================================
# TOOL REGISTRY
# =============================================================================

TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {
   "generate_dynamic_chart": {
        "function": generate_dynamic_chart,
        "scope": "tools:read",
        "description": "Create a dynamic chart/graph visualization from generic tabular data. Automatically selects the best chart type ('bar', 'line', 'pie', 'scatter') based on data heuristics, or accepts a specific chart type. Supports plotting specific X and Y axes, or frequency counts if no Y axis is provided.",
        "input_schema": {
            "type": "object",
            "properties": {
                "data": {
                    "type": "array",
                    "items": {
                        "type": "object"
                    },
                    "description": "The dataset to visualize, formatted as a list of dictionaries (e.g., [{'category': 'A', 'value': 10}, ...])."
                },
                "x_key": {
                    "type": "string",
                    "description": "The dictionary key in the data to use for the X-axis (labels/categories)."
                },
                "y_key": {
                    "type": "string",
                    "description": "The dictionary key in the data for the Y-axis values. If omitted or null, the tool will plot the frequency count of the x_key categories."
                },
                "chart_type": {
                    "type": "string",
                    "description": "The type of chart to render. Options: 'auto', 'bar', 'line', 'pie', or 'scatter'.",
                    "default": "auto"
                },
                "title": {
                    "type": "string",
                    "description": "The display title of the generated chart.",
                    "default": "Data Visualization"
                }
            },
            "required": ["data", "x_key"]
        }
    },
    "get_system_status": {
        "function": get_system_status,
        "scope": "tools:system",
        "description": (
            "Take a one-time SNAPSHOT of system status (CPU, memory, disk). "
            "For LIVE / continuously-updating system metrics — including "
            "any 'show me', 'monitor', 'watch', 'dashboard', 'live', or "
            "'real-time' system request — prefer `live_system_metrics` "
            "instead. Only use this tool when the user explicitly asks for "
            "a snapshot or one-time check."
        ),
        "input_schema": {
            "type": "object",
            "properties": {}
        },
        "streamable": {"default_interval": 2, "min_interval": 1, "max_interval": 30}
    },
    "get_cpu_info": {
        "function": get_cpu_info,
        "scope": "tools:system",
        "description": (
            "Take a one-time SNAPSHOT of detailed CPU information including "
            "per-core usage. For LIVE CPU monitoring use `live_system_metrics`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {}
        },
        "streamable": {"default_interval": 2, "min_interval": 1, "max_interval": 30}
    },
    "get_memory_info": {
        "function": get_memory_info,
        "scope": "tools:system",
        "description": (
            "Take a one-time SNAPSHOT of detailed memory (RAM + swap) "
            "information. For LIVE memory monitoring use `live_system_metrics`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {}
        },
        "streamable": {"default_interval": 2, "min_interval": 1, "max_interval": 30}
    },
    "get_disk_info": {
        "function": get_disk_info,
        "scope": "tools:system",
        "description": (
            "Take a one-time SNAPSHOT of disk partition and usage information. "
            "For LIVE disk usage monitoring use `live_system_metrics`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {}
        },
        "streamable": {"default_interval": 5, "min_interval": 2, "max_interval": 60}
    },
    # 001-tool-stream-ui: push-streaming counterpart to the four poll-based
    # system tools above. Single consolidated Card with CPU + memory + disk
    # that updates in place every interval_s seconds. Use this for live
    # dashboards instead of the snapshot-style get_system_status.
    "live_system_metrics": {
        "function": live_system_metrics,
        "scope": "tools:system",
        "description": (
            "STREAM LIVE CPU, memory, and disk usage. Pushes a fresh "
            "system status card every interval_s seconds, updating the "
            "same component in place. **PREFER THIS over `get_system_status` "
            "for any request that mentions 'live', 'real-time', 'monitor', "
            "'watch', 'dashboard', or 'show me' system metrics.** Use "
            "`get_system_status` only when the user explicitly asks for a "
            "one-time snapshot."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "interval_s": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 30,
                    "default": 2,
                    "description": "How often to emit a fresh sample (seconds)",
                },
            },
        },
        "metadata": {
            "streamable": True,
            "streaming_kind": "push",
            "max_fps": 2,
            "min_fps": 1,
            "max_chunk_bytes": 65536,
        },
    },
    "search_wikipedia": {
        "function": search_wikipedia,
        "scope": "tools:search",
        "description": "Search Wikipedia for articles and summaries on any topic.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "language": {"type": "string", "description": "Wikipedia language code", "default": "en"}
            },
            "required": ["query"]
        }
    },
    "search_arxiv": {
        "function": search_arxiv,
        "scope": "tools:search",
        "description": "Search arXiv for academic papers and research.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "description": "Maximum number of results", "default": 10}
            },
            "required": ["query"]
        }
    },
    "modify_data": {
        "function": modify_data,
        "scope": "tools:write",
        "description": "Modify CSV/Excel data with basic CRUD operations like dropping columns as well as row-based calculations. Supports add_column, update_column, calculate_column, drop_column, rename_column, filter_rows, and sort_rows. For files uploaded via the chat composer, pass the attachment_id as 'file_handle'; for tiny pasted data use 'csv_data'. To inspect/analyze an uploaded file (instead of mutating it), use read_spreadsheet first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "csv_data": {"type": "string", "description": "Raw CSV string data (use only for small/pasted data)"},
                "file_handle": {"type": "string", "description": "AstralDeep attachment_id from the chat composer (preferred for uploaded files)"},
                "modifications": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "description": "Action to perform: 'add_column', 'update_column', 'calculate_column', 'drop_column', 'rename_column', 'filter_rows', 'sort_rows'"},
                            "name": {"type": "string", "description": "Column name (can be empty for filter_rows)"},
                            "value": {"type": "string", "description": "Static value, or new_name for rename_column, or 'asc'/'desc' for sort_rows"},
                            "expression": {"type": "string", "description": "Python-like expression using row['column'] for per‑row calculation (e.g., \"row['age'] * 2\" or \"row['age'] > 18\" for filtering)"},
                            "default": {"type": "string", "description": "Fallback value if expression evaluation fails"},
                            "overwrite": {"type": "boolean", "description": "Whether to overwrite existing column (default true)"},
                            "dtype": {"type": "string", "description": "Data type for conversion: 'string', 'integer', 'float', 'boolean'"}
                        },
                        "required": ["action"]
                    }
                },
                "filename": {"type": "string", "description": "Optional name for the result file"},
                "output_format": {"type": "string", "description": "Output file format: 'csv' or 'excel' (defaults to input format)"}
            },
            "required": ["modifications"]
        }
    },
    "change_theme": {
        "function": change_theme,
        "scope": "tools:write",
        "description": "Show an interactive theme customization panel with preset buttons and color pickers. Use 'apply_theme_preset' or 'set_theme_color' instead when the user has already specified what they want.",
        "input_schema": {
            "type": "object",
            "properties": {
                "preset": {
                    "type": "string",
                    "description": "Optional preset name to show as default: midnight, daylight, ocean, sunset, forest"
                }
            }
        }
    },
    "apply_theme_preset": {
        "function": apply_theme_preset,
        "scope": "tools:write",
        "description": "Apply a predefined theme preset immediately. Available presets: midnight (dark indigo/purple), daylight (light mode), ocean (deep blue/cyan), sunset (warm orange/red), forest (green nature). Use this when the user asks to switch to a specific theme.",
        "input_schema": {
            "type": "object",
            "properties": {
                "preset": {
                    "type": "string",
                    "enum": ["midnight", "daylight", "ocean", "sunset", "forest"],
                    "description": "The theme preset name to apply"
                }
            },
            "required": ["preset"]
        }
    },
    "set_theme_color": {
        "function": set_theme_color,
        "scope": "tools:write",
        "description": "Change a single UI theme color. Valid color keys: bg (background), surface (card/panel backgrounds), primary (primary buttons and interactive elements), secondary (secondary buttons and elements), text (main text color), muted (secondary/muted text), accent (accent highlights). Value must be a hex color like '#FF5500'. Use this when the user asks to change a specific color.",
        "input_schema": {
            "type": "object",
            "properties": {
                "color_key": {
                    "type": "string",
                    "enum": ["bg", "surface", "primary", "secondary", "text", "muted", "accent"],
                    "description": "Which color to change: bg, surface, primary, secondary, text, muted, or accent"
                },
                "hex_value": {
                    "type": "string",
                    "description": "Hex color value (e.g. '#6366F1', '#FF5500')"
                }
            },
            "required": ["color_key", "hex_value"]
        }
    },
}


# =============================================================================
# File-handling tools (feature 002-file-uploads)
# =============================================================================
# Registered out-of-line to keep the diff against the main TOOL_REGISTRY tight
# and to make it easy to iterate on the file-tool surface without touching the
# rest of the registry.

from agents.general.file_tools.read_document import read_document as _read_document  # noqa: E402
from agents.general.file_tools.read_spreadsheet import read_spreadsheet as _read_spreadsheet  # noqa: E402
from agents.general.file_tools.read_presentation import read_presentation as _read_presentation  # noqa: E402
from agents.general.file_tools.read_text import read_text as _read_text  # noqa: E402
from agents.general.file_tools.read_image import read_image as _read_image  # noqa: E402
from agents.general.file_tools.list_attachments import list_attachments as _list_attachments  # noqa: E402
from agents.general.file_tools.medical import (  # noqa: E402
    compute_volume_statistics as _compute_volume_statistics,
    extract_volume_slice as _extract_volume_slice,
    extract_wsi_region as _extract_wsi_region,
    read_bio_tiff as _read_bio_tiff,
    read_czi as _read_czi,
    read_dicom as _read_dicom,
    read_nifti as _read_nifti,
    read_volume_itk as _read_volume_itk,
    read_wsi as _read_wsi,
)

TOOL_REGISTRY.update({
    "read_document": {
        "function": _read_document,
        "scope": "tools:files",
        "description": (
            "Read a document attachment (PDF, DOCX, RTF, ODT) the user has uploaded. "
            "Returns extracted text. PDFs without selectable text are rasterized and the "
            "page images are returned in the `images` field for the vision-capable model "
            "to interpret directly (no OCR step)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "attachment_id": {"type": "string", "format": "uuid"},
                "page_range": {"type": "string", "description": "Optional. e.g. '1-5,9'. PDFs only."},
                "max_chars": {"type": "integer", "minimum": 1, "default": 200000},
            },
            "required": ["attachment_id"],
        },
    },
    "read_spreadsheet": {
        "function": _read_spreadsheet,
        "scope": "tools:files",
        "description": (
            "Read a spreadsheet attachment (XLSX, XLS, ODS, TSV, CSV) the user has uploaded. "
            "Returns columns, rows, and (for multi-sheet workbooks) the available sheet names."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "attachment_id": {"type": "string", "format": "uuid"},
                "sheet_name": {"type": "string"},
                "max_rows": {"type": "integer", "minimum": 1, "default": 1000},
            },
            "required": ["attachment_id"],
        },
    },
    "read_presentation": {
        "function": _read_presentation,
        "scope": "tools:files",
        "description": (
            "Read a presentation attachment (PPTX or ODP) the user has uploaded. "
            "Returns each slide's title, body text, and speaker notes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "attachment_id": {"type": "string", "format": "uuid"},
                "slide_range": {"type": "string", "description": "Optional. e.g. '1-5,9'."},
            },
            "required": ["attachment_id"],
        },
    },
    "read_text": {
        "function": _read_text,
        "scope": "tools:files",
        "description": (
            "Read a text-class attachment (TXT, MD, JSON, YAML, XML, HTML, LOG, code) "
            "the user has uploaded. Returns the raw source plus a stripped plaintext "
            "rendering for HTML/XML."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "attachment_id": {"type": "string", "format": "uuid"},
                "max_chars": {"type": "integer", "minimum": 1, "default": 200000},
            },
            "required": ["attachment_id"],
        },
    },
    "read_image": {
        "function": _read_image,
        "scope": "tools:files",
        "description": (
            "Read an image attachment (PNG, JPG, GIF, WEBP) the user has uploaded "
            "and return base64-encoded bytes ready for delivery to a vision model."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "attachment_id": {"type": "string", "format": "uuid"},
            },
            "required": ["attachment_id"],
        },
    },
    "list_attachments": {
        "function": _list_attachments,
        "scope": "tools:files",
        "description": (
            "Enumerate the calling user's uploaded attachments. Useful when the user "
            "refers to a file uploaded earlier in a different chat."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["document", "spreadsheet", "presentation", "text", "image", "medical"],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            },
        },
    },
    "read_dicom": {
        "function": _read_dicom,
        "scope": "tools:files",
        "description": (
            "Read a DICOM medical-imaging file (.dcm, .dicom) the user has uploaded. "
            "Returns non-identifying metadata (modality, acquisition params, dimensions), "
            "pixel intensity stats, and a base64 PNG thumbnail of the pixel data. "
            "Patient-identifying fields (name, MRN, DOB, study/series dates, "
            "institution, physician) are stripped by default; pass include_phi=true "
            "ONLY when the user has authorised disclosure (e.g. pre-de-identified research data)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "attachment_id": {"type": "string", "format": "uuid"},
                "include_phi": {
                    "type": "boolean",
                    "default": False,
                    "description": "Surface patient-identifying tags alongside the safe metadata.",
                },
            },
            "required": ["attachment_id"],
        },
    },
    "read_nifti": {
        "function": _read_nifti,
        "scope": "tools:files",
        "description": (
            "Read a NIfTI neuroimaging volume (.nii, .nii.gz) the user has uploaded. "
            "Returns shape, voxel sizes, orientation, affine, pixel stats, and three "
            "orthogonal mid-plane thumbnails (axial, coronal, sagittal) as base64 PNGs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "attachment_id": {"type": "string", "format": "uuid"},
            },
            "required": ["attachment_id"],
        },
    },
    "read_czi": {
        "function": _read_czi,
        "scope": "tools:files",
        "description": (
            "Read a Zeiss CZI microscopy file (.czi) the user has uploaded. "
            "Returns dimension layout (STCZYX), pixel type, mosaic flag, and a "
            "mid-plane thumbnail of the requested scene."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "attachment_id": {"type": "string", "format": "uuid"},
                "scene": {"type": "integer", "minimum": 0, "default": 0},
            },
            "required": ["attachment_id"],
        },
    },
    "read_bio_tiff": {
        "function": _read_bio_tiff,
        "scope": "tools:files",
        "description": (
            "Read a TIFF or OME-TIFF file (.tif, .tiff, .ome.tif, .ome.tiff) the user "
            "has uploaded. Returns series layout, pyramid level count, OME-XML preview "
            "(if present), and a thumbnail of series 0 at the coarsest pyramid level."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "attachment_id": {"type": "string", "format": "uuid"},
            },
            "required": ["attachment_id"],
        },
    },
    "read_volume_itk": {
        "function": _read_volume_itk,
        "scope": "tools:files",
        "description": (
            "Read an NRRD or MetaImage volume (.nrrd, .mha, .mhd) the user has "
            "uploaded. Returns size, spacing, origin, direction cosines, per-voxel "
            "metadata, and a middle-slice thumbnail."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "attachment_id": {"type": "string", "format": "uuid"},
            },
            "required": ["attachment_id"],
        },
    },
    "read_wsi": {
        "function": _read_wsi,
        "scope": "tools:files",
        "description": (
            "Read a whole-slide pathology image (.svs, .ndpi) the user has uploaded. "
            "Returns pyramid level dimensions, downsample factors, microns-per-pixel, "
            "vendor-specific properties, and the slide's embedded thumbnail as base64 PNG. "
            "Use extract_wsi_region for high-magnification detail."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "attachment_id": {"type": "string", "format": "uuid"},
            },
            "required": ["attachment_id"],
        },
    },
    "extract_volume_slice": {
        "function": _extract_volume_slice,
        "scope": "tools:files",
        "description": (
            "Render a single 2-D slice of a volumetric medical file (DICOM multi-frame, "
            "NIfTI, NRRD/MHA/MHD, volumetric TIFF) as a base64 PNG. Call this after the "
            "corresponding read_* tool when the user asks for a specific slice."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "attachment_id": {"type": "string", "format": "uuid"},
                "axis": {
                    "type": "string",
                    "enum": ["x", "y", "z"],
                    "default": "z",
                    "description": "Which axis to slice along.",
                },
                "index": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "0-based slice index. Defaults to the middle of the axis.",
                },
            },
            "required": ["attachment_id"],
        },
    },
    "extract_wsi_region": {
        "function": _extract_wsi_region,
        "scope": "tools:files",
        "description": (
            "Crop a rectangular region from a whole-slide image at a specified pyramid "
            "level. Returns a base64 PNG capped at 2048x2048. Coordinates x,y are in "
            "level-0 reference pixels (OpenSlide convention)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "attachment_id": {"type": "string", "format": "uuid"},
                "level": {"type": "integer", "minimum": 0, "default": 0},
                "x": {"type": "integer", "default": 0},
                "y": {"type": "integer", "default": 0},
                "width": {"type": "integer", "minimum": 1, "maximum": 2048, "default": 512},
                "height": {"type": "integer", "minimum": 1, "maximum": 2048, "default": 512},
            },
            "required": ["attachment_id"],
        },
    },
    "compute_volume_statistics": {
        "function": _compute_volume_statistics,
        "scope": "tools:files",
        "description": (
            "Compute an intensity histogram and max-intensity projections (axial, "
            "coronal, sagittal) for a volumetric medical file. Returns bin edges + "
            "counts plus three thumbnail PNGs. Use when the user wants to understand "
            "the distribution of voxel values or see overall structure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "attachment_id": {"type": "string", "format": "uuid"},
                "bins": {"type": "integer", "minimum": 2, "maximum": 512, "default": 64},
            },
            "required": ["attachment_id"],
        },
    },
})
