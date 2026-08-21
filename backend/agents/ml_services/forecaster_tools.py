#!/usr/bin/env python3
"""Timeseries Forecaster tools for the ML Services agent (ported from ``agents/forecaster``).

Wraps the user-supplied Forecaster deployment (e.g. ``forecaster.ai.uky.edu``)
following its documented API contract:

- ``forecaster_submit_dataset``     — POST /dataset/submit             (returns uuid + columns)
- ``set_column_roles``              — POST /dataset/save-columns       (maps columns to one of 7 roles)
- ``forecaster_start_training_job`` — POST /dataset/start-training-job (LONG-RUNNING)
- ``forecaster_get_job_status``     — GET  /dataset/get-job-status     (sync status probe)
- ``forecaster_get_results``        — GET  /results/get-metrics        (metrics + output_log)
- ``forecaster_delete_dataset``     — POST /dataset/delete             (cleanup)
- ``_credentials_check``            — GET  /dataset/get-job-status (no params; internal auth
                                      probe dispatched per-bundle by the union registry)

The five verbs Forecaster shared with CLASSify carry the ``forecaster_``
prefix in the consolidated registry; behavior, input schemas, scopes, and
output components are unchanged from the originals. The training pipeline is
split into three steps (submit → set-roles → start) so the chat LLM can
converse with the user between steps before kicking off the long-running job.
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

# Roles the Forecaster service requires for column categorization (see
# forecaster-api-docs.md). Any column not assigned to one of these falls into
# `not-included` by default.
COLUMN_ROLES: List[str] = [
    "not-included",
    "time-component",
    "grouping",
    "target",
    "past-covariates",
    "future-covariates",
    "static-covariates",
]

# Documented defaults from forecaster-api-docs.md. The agent sends *only* the
# caller's overrides (sparse dict); the upstream uses its own defaults for any
# key not present. This dict is here for documentation + as a fallback so the
# LLM/UI can show users what the defaults are if they ask.
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
    """Build an HTTP client scoped to the Forecaster credential bundle.

    Args:
        credentials: Decrypted credential map containing ``FORECASTER_URL``
            and ``FORECASTER_API_KEY``.

    Returns:
        An (unvalidated) :class:`~agents.ml_services._wrapper.ExternalServiceClient`.
    """
    return _wrapper.ExternalServiceClient(credentials, BUNDLE)


def _build_client(kwargs: Dict[str, Any]) -> _wrapper.ExternalServiceClient:
    """Resolve and validate the Forecaster client from tool kwargs.

    Args:
        kwargs: The tool call's ``**kwargs`` carrying ``_credentials``.

    Returns:
        A validated client.

    Raises:
        ValueError: When credentials are absent, stale, or incomplete.
    """
    return _wrapper.build_client(kwargs, BUNDLE)


def _user_facing_error(exc: Exception, service: str = "Forecaster") -> str:
    """Map an HTTP-egress exception to the user-facing chat-rendered string.

    Args:
        exc: The exception raised by the upstream call.
        service: Service label for the message; defaults to ``"Forecaster"``.

    Returns:
        A one-line actionable error message.
    """
    return _wrapper.user_facing_error(exc, service)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _credentials_check(**kwargs) -> Dict[str, Any]:
    """Cheap GET to confirm the saved Forecaster URL and API key work.

    Calls ``GET /dataset/get-job-status`` with **no parameters**. The live
    forecaster.ai.uky.edu service returns 200 with ``{success: false,
    "message": "A UUID must be provded"}`` in that case — authentication has
    already been verified by then (a bogus key returns 401 before any body
    handling). Sending no uuid avoids the upstream's "500 on bad uuid lookup"
    bug exhibited when you pass a sentinel uuid.

    Args:
        **kwargs: Tool kwargs carrying ``_credentials``.

    Returns:
        A ``{"credential_test": ...}`` verdict dict.
    """
    try:
        client = _build_client(kwargs)
    except ValueError as e:
        return {"credential_test": "unexpected", "detail": str(e)}
    try:
        client.get("/dataset/get-job-status")
        return {"credential_test": "ok"}
    except BadRequestError:
        # 4xx-non-auth: route is reachable, auth was accepted.
        return {"credential_test": "ok"}
    except ExternalHttpError as e:
        return _wrapper.verdict_for_exception(e)


def forecaster_submit_dataset(file_handle: Optional[str] = None,
                              inline_data: Optional[str] = None,
                              **kwargs):
    """Upload a CSV dataset to the Forecaster service.

    Returns the upstream ``uuid`` plus the list of column names. The chat
    LLM uses the returned columns to converse with the user about which
    column is the time component, which is the target, and which (if any)
    are past/future/static covariates before calling ``set_column_roles``.

    Args:
        file_handle: Attachment handle of a CSV uploaded via AstralDeep.
        inline_data: Raw CSV text the user pasted in chat. Used only when
            ``file_handle`` is absent: materialized into a real attachment
            owned by the authenticated user (the orchestrator-injected
            ``user_id`` kwarg — never model-supplied), then processed
            exactly as if that handle had been passed.
        **kwargs: Tool kwargs (``_credentials``, ``user_id``).

    Returns:
        An MCP UI response dict with a Card (column Table) and ``_data``.
    """
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
    """Convert ``{column_name: role}`` to the role-keyed shape Forecaster wants.

    The upstream API expects ``categorizedString`` as a JSON-encoded dict keyed
    by role, with each value being the list of columns assigned to that role.
    The LLM-facing tool accepts the much friendlier inverse (one entry per
    column) and we convert here.

    Args:
        column_roles: Map of ``column_name → role``.

    Returns:
        A role-keyed dict covering every documented role (possibly empty lists).

    Raises:
        ValueError: On an empty/non-dict input or an unknown role, so the LLM
            gets a targeted error instead of a 4xx from upstream.
    """
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
    """Assign each column to one of the seven roles Forecaster understands.

    ``column_roles`` is a friendly ``{column_name: role}`` dict; the agent
    internally converts it to the role-keyed JSON shape required by
    ``POST /dataset/save-columns``. Columns omitted from ``column_roles``
    fall into ``not-included`` by upstream default.

    Allowed roles: ``not-included``, ``time-component``, ``grouping``,
    ``target``, ``past-covariates``, ``future-covariates``, ``static-covariates``.

    Args:
        uuid: Dataset UUID returned by ``forecaster_submit_dataset``.
        column_roles: Map of ``column_name → role``.
        **kwargs: Tool kwargs (``_credentials``).

    Returns:
        An MCP UI response dict with a role-summary Card and ``_data``.
    """
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
        # Summarize for the chat: list every role that has at least one
        # column assigned, biggest first.
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
    """Build a sync callable that probes ``/dataset/get-job-status`` for one job.

    Normalizes the upstream status string into the JobPoller's vocabulary:
        "Completed"           → succeeded (+ fetches /results/get-metrics)
        contains "Training"   → in_progress
        any other non-empty   → in_progress (defensive — covers "Initializing"/"Queued"/etc.)
        empty / missing       → failed

    Args:
        client: A validated Forecaster HTTP client.
        uuid: The job's dataset UUID.

    Returns:
        A zero-arg callable returning the normalized status dict.
    """
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
    """Start a Forecaster training job and register the JobPoller.

    Returns immediately with the ``uuid`` and ``status: "started"``. The
    agent's JobPoller posts ``tool_progress`` messages into the chat as the
    job runs and a terminal message with metrics on completion.

    ``options`` is a sparse dict of overrides over the upstream defaults
    (which are documented in ``DEFAULT_TRAINING_OPTIONS``). The agent passes
    the dict through unchanged; the upstream applies its own defaults for any
    unspecified key. To run a fast smoke test, pass e.g.
    ``{"models": ["linear-regression"], "epochs": 1}``.

    Args:
        uuid: Dataset UUID from ``forecaster_submit_dataset``.
        options: Sparse override dict (upstream defaults apply otherwise).
        **kwargs: Tool kwargs (``_credentials``, ``_runtime``).

    Returns:
        An MCP UI response dict with a started-confirmation Card and ``_data``.
    """
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
    """Synchronously probe the status of a Forecaster job by UUID.

    Args:
        uuid: The job's dataset UUID.
        **kwargs: Tool kwargs (``_credentials``).

    Returns:
        An MCP UI response dict with a status Card and ``_data``.
    """
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
    """Fetch the final metrics + output_log for a completed Forecaster job.

    Per the Forecaster API docs, ``/results/get-metrics`` returns:
        { "output_log": "...", "file_contents": <metrics JSON> }

    Shape-aware rendering of file_contents:
        * ``{model: {metric: value, ...}, ...}`` → one row per model, columns = metric names
        * ``{metric: value, ...}`` (flat)        → two-column Metric | Value table
        * anything else (text, mixed nesting)   → fall back to truncated JSON Text block

    Args:
        uuid: The job's dataset UUID.
        **kwargs: Tool kwargs (``_credentials``).

    Returns:
        An MCP UI response dict with results Card(s) and ``_data``.
    """
    try:
        client = _build_client(kwargs)
        resp = client.get("/results/get-metrics", params={"uuid": uuid})
        payload = _safe_json(resp)
        output_log = payload.get("output_log", "") if isinstance(payload, dict) else ""
        metrics = payload.get("file_contents") if isinstance(payload, dict) else None
        # The live service encodes file_contents as a JSON *string*, not an
        # object. Parse it so per-model rendering works; if it's already a
        # dict (or doesn't parse), leave it alone.
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
    """Delete a Forecaster dataset and all of its associated models / artifacts.

    Args:
        uuid: The dataset UUID to delete.
        **kwargs: Tool kwargs (``_credentials``).

    Returns:
        An MCP UI response dict with a confirmation Card and ``_data``.
    """
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


# ---------------------------------------------------------------------------
# Tool registry (Forecaster slice — merged into the union by mcp_tools)
# ---------------------------------------------------------------------------


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
