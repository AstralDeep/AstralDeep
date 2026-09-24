#!/usr/bin/env python3
"""Forecaster tool slice for the ML Services agent:
submit/configure/train/poll/fetch-results/delete against a user's Forecaster
deployment via _wrapper.py's ExternalServiceClient; merged into the union registry by
mcp_tools.py.
"""
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Set

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from shared.attachment_materializer import materialize_text_attachment
from shared.attachment_resolver import open_attachment_blob_reader
from shared.external_http import BadRequestError, ExternalHttpError
from astralprims import Alert, Card, Table, Text

from agents.ml_services import _wrapper
from agents.ml_services._wrapper import (
    FORECASTER_BUNDLE as BUNDLE,
    render_metric_value as _render_metric_value,
    safe_json as _safe_json,
    ui as _ui,
)

logger = logging.getLogger("MlServicesForecasterTools")

LONG_RUNNING_TOOLS: Set[str] = {"forecaster_start_training_job"}

COLUMN_ROLES: List[str] = [
    "not-included",
    "time-component",
    "grouping",
    "target",
    "past-covariates",
    "future-covariates",
    "static-covariates",
]

DEFAULT_TRAINING_OPTIONS: Dict[str, Any] = {
    "test-size": 0.2,
    "expanding-window": False,
    "expanding-window-forecast-horizon": 6,
    "expanding-window-stride": 6,
    "visualize": True,
    "generate-real-predictions": False,
    "real-prediction-length": 12,
    "fill-future-covariates": False,
    "probabilistic": False,
    "probabilistic-likelihood": "quantile",
    "probabilistic-num-samples": 100,
    "models": [
        "arima", "exponential-smoothing", "linear-regression", "xgboost",
        "random-forest", "lgbm", "nlinear", "tft",
    ],
    "arima-p": 12,
    "arima-d": 1,
    "arima-q": 6,
    "lags": 12,
    "lags-future": 0,
    "output-chunk-length": 6,
    "epochs": 10,
}


def make_client(credentials: Dict[str, str]) -> _wrapper.ExternalServiceClient:
    return _wrapper.ExternalServiceClient(credentials, BUNDLE)


def _build_client(kwargs: Dict[str, Any]) -> _wrapper.ExternalServiceClient:
    return _wrapper.build_client(kwargs, BUNDLE)


def _user_facing_error(exc: Exception, service: str = "Forecaster") -> str:
    return _wrapper.user_facing_error(exc, service)


def _credentials_check(**kwargs) -> Dict[str, Any]:
    try:
        client = _build_client(kwargs)
    except ValueError as e:
        return {"credential_test": "unexpected", "detail": str(e)}
    try:
        client.get("/dataset/get-job-status")
        return {"credential_test": "ok"}
    except BadRequestError:
        return {"credential_test": "ok"}
    except ExternalHttpError as e:
        return _wrapper.verdict_for_exception(e)


def forecaster_submit_dataset(file_handle: Optional[str] = None,
                              inline_data: Optional[str] = None,
                              **kwargs):
    try:
        client = _build_client(kwargs)
        user_id = kwargs.get("user_id")
        if not user_id:
            raise ValueError("user_id is required to resolve attachments.")
        if not file_handle and inline_data:
            file_handle = materialize_text_attachment(inline_data, user_id, extension="csv")
            logger.info(
                "forecaster_submit_dataset: materialized inline_data into attachment %s for user=%s",
                file_handle, user_id,
            )
        if not file_handle:
            raise ValueError(
                "Provide either file_handle (the attachment_id of an uploaded "
                "CSV) or inline_data (the raw CSV text the user pasted in chat)."
            )
        with open_attachment_blob_reader(file_handle, user_id) as (attachment, reader):
            filename = attachment.filename
            payload_bytes = b"".join(reader.iter_chunks())
        resp = client.post(
            "/dataset/submit",
            files={"file": (filename, payload_bytes, "text/csv")},
        )
        payload = _safe_json(resp)
        uuid = payload.get("uuid")
        columns = payload.get("columns") or []
        if not isinstance(columns, list):
            columns = []
        header = (
            f"Dataset UUID: `{uuid}`\n\n"
            f"Detected **{len(columns)} column(s)**. Next, call "
            "`set_column_roles` to map each column to a role "
            "(time-component, target, past-covariates, etc.)."
        )
        columns_table = Table(
            headers=["#", "Column"],
            rows=[[str(i + 1), col] for i, col in enumerate(columns)],
        )
        return _ui(
            [Card(
                title=f"Dataset uploaded: {filename}",
                content=[Text(content=header), columns_table],
            )],
            data={
                "uuid": uuid,
                "columns": columns,
                "filename": filename,
                "allowed_roles": COLUMN_ROLES,
            },
        )
    except (ExternalHttpError, ValueError) as e:
        return _ui([Alert(message=_user_facing_error(e), variant="error")], retryable=False)


def _build_categorized_string(column_roles: Dict[str, str]) -> Dict[str, List[str]]:
    if not isinstance(column_roles, dict) or not column_roles:
        raise ValueError(
            "column_roles must be a non-empty dict of {column_name: role}."
        )
    categorized: Dict[str, List[str]] = {role: [] for role in COLUMN_ROLES}
    for col, role in column_roles.items():
        if role not in categorized:
            raise ValueError(
                f"Unknown column role {role!r} for column {col!r}. "
                f"Allowed roles: {', '.join(COLUMN_ROLES)}."
            )
        categorized[role].append(col)
    return categorized


def set_column_roles(uuid: str, column_roles: Dict[str, str], **kwargs):
    try:
        client = _build_client(kwargs)
        categorized = _build_categorized_string(column_roles)
        resp = client.post(
            "/dataset/save-columns",
            data={
                "categorizedString": json.dumps(categorized),
                "uuid": uuid,
            },
        )
        payload = _safe_json(resp)
        nonempty = {role: cols for role, cols in categorized.items() if cols}
        rows = [[role, ", ".join(cols)] for role, cols in nonempty.items()]
        summary_table = Table(headers=["Role", "Columns"], rows=rows)
        return _ui(
            [Card(
                title="Column roles saved",
                content=[
                    Text(content=f"Saved column roles for dataset `{uuid}`."),
                    summary_table,
                ],
            )],
            data={
                "uuid": uuid,
                "categorized": categorized,
                "response": payload,
            },
        )
    except (ExternalHttpError, ValueError) as e:
        return _ui([Alert(message=_user_facing_error(e), variant="error")], retryable=False)


def _make_status_poll(client: "_wrapper.ExternalServiceClient", uuid: str):
    def _poll():
        resp = client.get("/dataset/get-job-status", params={"uuid": uuid})
        payload = _safe_json(resp)
        raw = (payload.get("status") or "").strip()
        if raw == "Completed":
            try:
                metrics_resp = client.get("/results/get-metrics", params={"uuid": uuid})
                metrics_payload = _safe_json(metrics_resp) or metrics_resp.text
            except Exception:
                metrics_payload = None
            return {
                "status": "succeeded",
                "percentage": 100,
                "message": "Training complete.",
                "result": metrics_payload,
            }
        if "Training" in raw:
            return {"status": "in_progress", "percentage": None, "message": raw}
        if raw:
            return {"status": "in_progress", "percentage": None, "message": raw}
        return {
            "status": "failed",
            "percentage": None,
            "message": "Empty or missing status from upstream.",
            "result": None,
        }
    return _poll


def forecaster_start_training_job(uuid: str, options: Optional[Dict[str, Any]] = None, **kwargs):
    try:
        client = _build_client(kwargs)
        body_data: Dict[str, Any] = {"uuid": uuid}
        if options is not None:
            if not isinstance(options, dict):
                raise ValueError("options must be a dict of parameter overrides.")
            body_data["options"] = json.dumps(options)
        else:
            body_data["options"] = json.dumps({})
        resp = client.post("/dataset/start-training-job", data=body_data)
        payload = _safe_json(resp)
        runtime = kwargs.get("_runtime")
        if runtime is not None:
            runtime.start_long_running_job(_make_status_poll(client, uuid))
        return _ui(
            [Card(
                title="Forecaster training started",
                content=[Text(content=(
                    f"Dataset UUID: {uuid}\n"
                    "Progress will be posted in this chat as the job runs."
                ))],
            )],
            data={
                "uuid": uuid,
                "status": "started",
                "options": options or {},
                "upstream_response": payload,
                "message": "Training started. Progress will appear here automatically.",
            },
        )
    except (ExternalHttpError, ValueError) as e:
        return _ui([Alert(message=_user_facing_error(e), variant="error")], retryable=False)


def forecaster_get_job_status(uuid: str, **kwargs):
    try:
        client = _build_client(kwargs)
        poll = _make_status_poll(client, uuid)
        result = poll()
        return _ui(
            [Card(
                title=f"Job {uuid}",
                content=[Text(content=(
                    f"Status: {result['status']}\n"
                    f"Message: {result.get('message') or '(none)'}"
                    + (f"\nPercentage: {result['percentage']}%" if result.get("percentage") is not None else "")
                ))],
            )],
            data={"uuid": uuid, **result},
        )
    except (ExternalHttpError, ValueError) as e:
        return _ui([Alert(message=_user_facing_error(e), variant="error")], retryable=False)


def forecaster_get_results(uuid: str, **kwargs):
    try:
        client = _build_client(kwargs)
        resp = client.get("/results/get-metrics", params={"uuid": uuid})
        payload = _safe_json(resp)
        output_log = payload.get("output_log", "") if isinstance(payload, dict) else ""
        metrics = payload.get("file_contents") if isinstance(payload, dict) else None
        if isinstance(metrics, str):
            try:
                metrics = json.loads(metrics)
            except (ValueError, TypeError):
                pass

        data = {"uuid": uuid, "output_log": output_log, "metrics": metrics}

        components: List[Any] = []

        if isinstance(metrics, dict) and metrics:
            if all(isinstance(v, dict) for v in metrics.values()):
                metric_keys: List[str] = []
                seen = set()
                for model_metrics in metrics.values():
                    for k in model_metrics.keys():
                        if k not in seen:
                            seen.add(k)
                            metric_keys.append(k)
                metric_keys.sort()
                rows = [
                    [model] + [_render_metric_value(m.get(k)) for k in metric_keys]
                    for model, m in metrics.items()
                ]
                table = Table(headers=["Model"] + metric_keys, rows=rows)
                components.append(Card(
                    title=f"Results for {uuid}",
                    content=[
                        Text(content=f"**{len(metrics)} model(s)**, **{len(metric_keys)} metric(s)**."),
                        table,
                    ],
                ))
            elif all(not isinstance(v, dict) for v in metrics.values()):
                rows = [[k, _render_metric_value(v)] for k, v in metrics.items()]
                table = Table(headers=["Metric", "Value"], rows=rows)
                components.append(Card(
                    title=f"Results for {uuid}",
                    content=[table],
                ))
            else:
                body = json.dumps(metrics, indent=2)[:4000]
                components.append(Card(
                    title=f"Results for {uuid}",
                    content=[Text(content=body)],
                ))
        else:
            body = (
                json.dumps(metrics, indent=2)[:4000]
                if metrics
                else (resp.text[:4000] if resp.content else "(no metrics returned)")
            )
            components.append(Card(
                title=f"Results for {uuid}",
                content=[Text(content=body)],
            ))

        if output_log:
            log_text = output_log if len(output_log) <= 4000 else output_log[:4000] + "\n… (truncated)"
            components.append(Card(
                title="Output log",
                content=[Text(content=log_text)],
            ))

        return _ui(components, data=data)
    except (ExternalHttpError, ValueError) as e:
        return _ui([Alert(message=_user_facing_error(e), variant="error")], retryable=False)


def forecaster_delete_dataset(uuid: str, **kwargs):
    try:
        client = _build_client(kwargs)
        resp = client.post("/dataset/delete", data={"uuid": uuid})
        payload = _safe_json(resp)
        return _ui(
            [Card(
                title="Dataset deleted",
                content=[Text(content=f"Dataset {uuid} has been removed from Forecaster.")],
            )],
            data={"uuid": uuid, "response": payload},
        )
    except (ExternalHttpError, ValueError) as e:
        return _ui([Alert(message=_user_facing_error(e), variant="error")], retryable=False)


TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "forecaster_submit_dataset": {
        "function": forecaster_submit_dataset,
        "description": (
            "Upload a CSV time-series dataset to the Forecaster service. Returns a "
            "dataset UUID and the list of column names from the file. Use the returned "
            "columns to ask the user which column is the time component, which is the "
            "target, and which (if any) are past/future/static covariates before "
            "calling set_column_roles. file_handle should be the attachment_id from "
            "the AstralDeep upload mechanism, NOT the display filename. "
            "If the user pasted data in chat, pass it via inline_data — NEVER invent "
            "a file_handle. "
            "DO NOT call read_spreadsheet/read_csv before this — "
            "forecaster_submit_dataset returns the column names directly. This is the "
            "FIRST step of the Forecaster pipeline."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_handle": {
                    "type": "string",
                    "description": "Handle of a CSV uploaded via AstralDeep's file mechanism.",
                },
                "inline_data": {
                    "type": "string",
                    "description": (
                        "Raw CSV text the user pasted in chat (markdown code fences "
                        "are stripped automatically; max 1 MB). Use this when no file "
                        "was uploaded instead of inventing a file_handle."
                    ),
                },
            },
            "required": [],
        },
        "scope": "tools:write",
    },
    "set_column_roles": {
        "function": set_column_roles,
        "description": (
            "Assign every column to one of the seven roles Forecaster understands. "
            "Pass column_roles as {column_name: role}. Allowed roles: "
            "'not-included', 'time-component' (the timestamp column — usually exactly one), "
            "'grouping' (e.g. region/store id if you have multiple parallel series), "
            "'target' (the value being forecast — usually exactly one), "
            "'past-covariates' (features known only up to the present), "
            "'future-covariates' (features known into the future, e.g. day-of-week), "
            "'static-covariates' (constant per series). Columns omitted from "
            "column_roles fall into 'not-included' by upstream default. "
            "Call this AFTER forecaster_submit_dataset and BEFORE "
            "forecaster_start_training_job."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "uuid": {
                    "type": "string",
                    "description": "Dataset UUID returned by submit_dataset.",
                },
                "column_roles": {
                    "type": "object",
                    "description": (
                        "Map of column_name → role. Example: "
                        "{'Date': 'time-component', 'Volume': 'target', "
                        "'Rain': 'past-covariates', 'Temp': 'past-covariates'}."
                    ),
                    "additionalProperties": {
                        "type": "string",
                        "enum": COLUMN_ROLES,
                    },
                },
            },
            "required": ["uuid", "column_roles"],
        },
        "scope": "tools:write",
    },
    "forecaster_start_training_job": {
        "function": forecaster_start_training_job,
        "description": (
            "Kick off a Forecaster training job on a dataset whose columns have already "
            "been categorized via set_column_roles. options is a sparse dict of "
            "overrides; any key not present uses the upstream default. Returns "
            "immediately with the UUID and posts progress + final metrics into the "
            "chat automatically as the job runs.\n\n"
            "Documented options (defaults shown):\n"
            "  test-size: 0.2\n"
            "  expanding-window: False\n"
            "  expanding-window-forecast-horizon: 6\n"
            "  expanding-window-stride: 6\n"
            "  visualize: True\n"
            "  generate-real-predictions: False  (set True for future forecasts beyond test)\n"
            "  real-prediction-length: 12         (used when generate-real-predictions=True)\n"
            "  fill-future-covariates: False\n"
            "  probabilistic: False\n"
            "  probabilistic-likelihood: 'quantile'\n"
            "  probabilistic-num-samples: 100\n"
            "  models: ['arima','exponential-smoothing','linear-regression','xgboost',"
            "'random-forest','lgbm','nlinear','tft']\n"
            "  arima-p: 12, arima-d: 1, arima-q: 6\n"
            "  lags: 12, lags-future: 0\n"
            "  output-chunk-length: 6\n"
            "  epochs: 10"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "uuid": {
                    "type": "string",
                    "description": "Dataset UUID from forecaster_submit_dataset.",
                },
                "options": {
                    "type": "object",
                    "description": (
                        "Sparse override dict; only include keys you want to differ "
                        "from the upstream defaults. See the tool description for the "
                        "full list of documented options."
                    ),
                    "additionalProperties": True,
                },
            },
            "required": ["uuid"],
        },
        "scope": "tools:write",
    },
    "forecaster_get_job_status": {
        "function": forecaster_get_job_status,
        "description": (
            "Synchronously probe the status of a Forecaster job by UUID. The poller "
            "usually pushes updates automatically; use this only for explicit user "
            "'did my job finish?' queries."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"uuid": {"type": "string"}},
            "required": ["uuid"],
        },
        "scope": "tools:read",
    },
    "forecaster_get_results": {
        "function": forecaster_get_results,
        "description": (
            "Fetch the final metrics + output_log for a completed Forecaster job. "
            "Renders per-model metrics as a table when available."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"uuid": {"type": "string"}},
            "required": ["uuid"],
        },
        "scope": "tools:read",
    },
    "forecaster_delete_dataset": {
        "function": forecaster_delete_dataset,
        "description": "Delete a Forecaster dataset and all of its associated models / artifacts.",
        "input_schema": {
            "type": "object",
            "properties": {"uuid": {"type": "string"}},
            "required": ["uuid"],
        },
        "scope": "tools:write",
        "metadata": {"external_target": "Forecaster"},
    },
}
