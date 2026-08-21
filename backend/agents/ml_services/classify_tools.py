#!/usr/bin/env python3
"""CLASSify tools for the ML Services agent (ported from ``agents/classify``).

Wraps the user-supplied CLASSify deployment:

- ``classify_submit_dataset``     — POST /reports/submit
- ``set_column_types``            — POST /reports/set-column-changes
- ``get_ml_options``              — GET  /reports/get-ml-opts
- ``classify_start_training_job`` — POST /reports/start-training-job  (long-running)
- ``classify_get_job_status``     — GET  /reports/get-job-status
- ``classify_get_results``        — GET  /result/get-results
- ``get_output_log``              — GET  /result/get-output-log
- ``classify_delete_dataset``     — POST /reports/delete
- ``_credentials_check``          — internal auth probe (GET /reports/get-ml-opts;
                                    dispatched per-bundle by the union registry)

The five verbs CLASSify shared with the Forecaster service carry the
``classify_`` prefix in the consolidated registry; behavior, input schemas,
scopes, and output components are unchanged from the originals. The training
pipeline is intentionally split across multiple tools so the chat LLM can
converse with the user between steps before kicking off the long-running job.
"""
import json
from io import BytesIO
import logging
import os
import re
import sys
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Set

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from shared.attachment_materializer import materialize_text_attachment
from shared.attachment_resolver import open_attachment_blob_reader
from shared.external_http import ExternalHttpError
from astralprims import Alert, Card, ParamPicker, Table, Text

from agents.ml_services import _wrapper
from agents.ml_services._wrapper import (
    CLASSIFY_BUNDLE as BUNDLE,
    render_metric_value as _render_metric_value,
    safe_json as _safe_json,
    ui as _ui,
)

logger = logging.getLogger("MlServicesClassifyTools")

LONG_RUNNING_TOOLS: Set[str] = {"classify_start_training_job"}

# In-process cache: report_uuid -> (attachment_id, owner_id). It deliberately
# retains no blob bytes; later calls repeat the metadata authorization check and
# open a fresh bounded owner-scoped Plane reader.
_REPORT_ATTACHMENTS: "OrderedDict[str, tuple[str, str]]" = OrderedDict()
_REPORT_ATTACHMENTS_MAX = 256


def _remember_report_attachment(
    report_uuid: str,
    attachment_id: str,
    owner_id: str,
) -> None:
    """Stash authorized identities so later tools can reopen a fresh lease.

    Args:
        report_uuid: Upstream report identifier returned at submit time.
        attachment_id: Typed attachment identity submitted upstream.
        owner_id: Authenticated attachment owner.
    """
    if not report_uuid or not attachment_id or not owner_id:
        return
    _REPORT_ATTACHMENTS[report_uuid] = (attachment_id, owner_id)
    _REPORT_ATTACHMENTS.move_to_end(report_uuid)
    while len(_REPORT_ATTACHMENTS) > _REPORT_ATTACHMENTS_MAX:
        _REPORT_ATTACHMENTS.popitem(last=False)


def _forget_report_attachment(report_uuid: str) -> None:
    """Drop the stashed attachment identities for a deleted report.

    Args:
        report_uuid: Upstream report identifier to evict from the cache.
    """
    _REPORT_ATTACHMENTS.pop(report_uuid, None)


def make_client(credentials: Dict[str, str]) -> _wrapper.ExternalServiceClient:
    """Build an HTTP client scoped to the CLASSify credential bundle.

    Args:
        credentials: Decrypted credential map containing ``CLASSIFY_URL`` and
            ``CLASSIFY_API_KEY``.

    Returns:
        An (unvalidated) :class:`~agents.ml_services._wrapper.ExternalServiceClient`.
    """
    return _wrapper.ExternalServiceClient(credentials, BUNDLE)


def _build_client(kwargs: Dict[str, Any]) -> _wrapper.ExternalServiceClient:
    """Resolve and validate the CLASSify client from tool kwargs.

    Args:
        kwargs: The tool call's ``**kwargs`` carrying ``_credentials``.

    Returns:
        A validated client.

    Raises:
        ValueError: When credentials are absent, stale, or incomplete.
    """
    return _wrapper.build_client(kwargs, BUNDLE)


def _user_facing_error(exc: Exception, service: str = "CLASSify") -> str:
    """Map an HTTP-egress exception to the user-facing chat-rendered string.

    Args:
        exc: The exception raised by the upstream call.
        service: Service label for the message; defaults to ``"CLASSify"``.

    Returns:
        A one-line actionable error message.
    """
    return _wrapper.user_facing_error(exc, service)


def _format_default_value(value: Any, max_length: int = 30) -> str:
    """Render a parameter default value as a compact, table-cell-friendly string.

    Args:
        value: The upstream default value of any type.
        max_length: Maximum rendered length before ellipsis truncation.

    Returns:
        A short display string (``—`` for ``None``).
    """
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (list, tuple)):
        text = ", ".join(str(v) for v in value)
    else:
        text = str(value)
    if len(text) > max_length:
        text = text[: max_length - 1] + "…"
    return text


def _format_models_list(models: Any, max_visible: int = 3) -> str:
    """Render a 'Applies to' cell, showing the first few entries + a count.

    Args:
        models: Upstream list of model names a parameter applies to.
        max_visible: How many entries to show before truncating with a count.

    Returns:
        A short display string (``—`` for empty / non-list input).
    """
    if not isinstance(models, (list, tuple)) or not models:
        return "—"
    visible = list(models)[:max_visible]
    rendered = ", ".join(str(m) for m in visible)
    if len(models) > max_visible:
        rendered += f", … and {len(models) - max_visible} more"
    return rendered


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _credentials_check(**kwargs) -> Dict[str, Any]:
    """Cheap GET to confirm the saved CLASSify URL and API key work.

    Args:
        **kwargs: Tool kwargs carrying ``_credentials``.

    Returns:
        A ``{"credential_test": ...}`` verdict dict (``ok`` / ``auth_failed``
        / ``unreachable`` / ``unexpected`` with optional ``detail``).
    """
    try:
        client = _build_client(kwargs)
    except ValueError as e:
        return {"credential_test": "unexpected", "detail": str(e)}
    try:
        resp = client.get("/reports/get-ml-opts", params={"unsstate": 0})
        if resp.status_code == 200:
            return {"credential_test": "ok"}
        return {"credential_test": "unexpected", "detail": f"HTTP {resp.status_code}"}
    except ExternalHttpError as e:
        return _wrapper.verdict_for_exception(e)


def classify_submit_dataset(file_handle: Optional[str] = None,
                            inline_data: Optional[str] = None,
                            **kwargs):
    """Upload a CSV dataset to CLASSify.

    Returns the upstream ``report_uuid`` plus the inferred column data types.
    The chat LLM uses the returned column types to converse with the user
    about which column is the class, which columns to drop, and how to
    handle missing values, before calling ``set_column_types``.

    Args:
        file_handle: Attachment handle of a CSV uploaded via AstralDeep.
        inline_data: Raw CSV text the user pasted in chat. Used only when
            ``file_handle`` is absent: materialized into a real attachment
            owned by the authenticated user (the orchestrator-injected
            ``user_id`` kwarg — never model-supplied), then processed
            exactly as if that handle had been passed.
        **kwargs: Tool kwargs (``_credentials``, ``user_id``, ``session_id``).

    Returns:
        An MCP UI response dict with a Card (column-type Table) and ``_data``.
    """
    try:
        client = _build_client(kwargs)
        user_id = kwargs.get("user_id")
        if not user_id:
            raise ValueError("user_id is required to resolve attachments")
        if not file_handle and inline_data:
            file_handle = materialize_text_attachment(inline_data, user_id, extension="csv")
            logger.info(
                "classify_submit_dataset: materialized inline_data into attachment %s for user=%s",
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
            "/reports/submit",
            files={"file": (filename, payload_bytes, "text/csv")},
        )
        payload = _safe_json(resp)
        report_uuid = payload.get("report_uuid")
        column_types_block = payload.get("column_types") or {}
        column_types = column_types_block.get("data_types") if isinstance(column_types_block, dict) else {}
        if not isinstance(column_types, dict):
            column_types = {}
        # Retain typed identities, never the lease path, so a later call can
        # repeat ownership validation and open a fresh scoped capability.
        if report_uuid:
            _remember_report_attachment(report_uuid, file_handle, user_id)
        header_text = (
            f"Report UUID: `{report_uuid}`\n\n"
            f"Detected **{len(column_types)} column(s)**:"
        )
        columns_table = Table(
            headers=["Column", "Detected type"],
            rows=[[col, dtype] for col, dtype in column_types.items()],
        )
        return _ui(
            [Card(
                title=f"Dataset uploaded: {filename}",
                content=[Text(content=header_text), columns_table],
            )],
            data={
                "report_uuid": report_uuid,
                "column_types": column_types,
                "filename": filename,
                "debug_copy_path": None,
            },
        )
    except (ExternalHttpError, ValueError) as e:
        return _ui([Alert(message=_user_facing_error(e), variant="error")], retryable=False)


def set_column_types(report_uuid: str,
                     file_handle: Optional[str] = None,
                     class_column: Optional[str] = None,
                     column_types: Optional[Dict[str, str]] = None,
                     missing_strategy: str = "synthetic",
                     fill_value: Any = None,
                     excluded_columns: Optional[List[str]] = None,
                     column_changes: Optional[List[Dict[str, Any]]] = None,
                     **kwargs):
    """Apply column-type / missing-value / class-column choices to a submitted dataset.

    Default (auto-build) path mirrors ``submission_example.startJob`` exactly:
    re-reads the CSV via pandas, flags ``missing`` per-column when nulls are
    present, marks the class column, and POSTs the resulting list. The LLM
    only needs to forward ``file_handle`` and ``column_types`` from
    ``classify_submit_dataset``'s response plus the chosen ``class_column``.

    Escape hatch: pass a pre-built ``column_changes`` list to bypass the
    pandas-driven build (useful for power-user overrides and round-trip tests).

    Args:
        report_uuid: Report UUID returned by ``classify_submit_dataset``.
        file_handle: Optional attachment handle fallback when the stashed
            path cache is cold.
        class_column: Name of the class column (required on the auto path).
        column_types: ``{column: dtype}`` map from the submit response.
        missing_strategy: ``synthetic`` / ``constant`` / ``none``.
        fill_value: Used only when ``missing_strategy='constant'``.
        excluded_columns: Columns to mark ``checked=False``.
        column_changes: Pre-built per-column list (bypasses auto-build).
        **kwargs: Tool kwargs (``_credentials``, ``user_id``).

    Returns:
        An MCP UI response dict with a confirmation Card and ``_data``.
    """
    try:
        client = _build_client(kwargs)
        if column_changes is not None:
            if not isinstance(column_changes, list):
                raise ValueError("column_changes must be a list of per-column dicts.")
            if class_column and not any(
                isinstance(c, dict) and c.get("class") is True for c in column_changes
            ):
                for entry in column_changes:
                    if isinstance(entry, dict) and entry.get("column") == class_column:
                        entry["class"] = True
                        break
        else:
            if not class_column:
                raise ValueError("class_column is required to auto-build column_changes.")
            cached = _REPORT_ATTACHMENTS.get(report_uuid)
            if cached is None:
                if not file_handle:
                    raise ValueError(
                        "No attachment on record for this report_uuid; call "
                        "classify_submit_dataset first, or pass the original file_handle."
                    )
                user_id = kwargs.get("user_id")
                if not user_id:
                    raise ValueError("user_id is required to resolve attachments.")
                cached = (file_handle, user_id)
            cached_attachment_id, cached_owner_id = cached
            with open_attachment_blob_reader(
                cached_attachment_id,
                cached_owner_id,
            ) as (_attachment, reader):
                df = pd.read_csv(BytesIO(b"".join(reader.iter_chunks())))
            if not isinstance(column_types, dict) or not column_types:
                column_types = {c: "string" for c in df.columns}
            excluded = set(excluded_columns or [])
            column_changes = []
            for col, dtype in column_types.items():
                has_missing = bool(df[col].isnull().any()) if col in df.columns else False
                entry = {
                    "column": col,
                    "data_type": dtype,
                    "checked": col not in excluded,
                    "missing": (missing_strategy if has_missing and missing_strategy != "none" else None),
                    "fill_value": (fill_value if (has_missing and missing_strategy == "constant") else None),
                }
                if col == class_column:
                    entry["class"] = True
                column_changes.append(entry)
        resp = client.post(
            "/reports/set-column-changes",
            data={
                "report_uuid": report_uuid,
                "column_changes": json.dumps(column_changes),
            },
        )
        payload = _safe_json(resp)
        return _ui(
            [Card(
                title="Column types saved",
                content=[Text(content=(
                    f"Configured {len(column_changes)} column(s) for report {report_uuid}."
                ))],
            )],
            data={"report_uuid": report_uuid, "response": payload,
                  "column_changes": column_changes},
        )
    except (ExternalHttpError, ValueError) as e:
        return _ui([Alert(message=_user_facing_error(e), variant="error")], retryable=False)


def get_ml_options(unsstate: int = 0, **kwargs):
    """List supported hyperparameter options for CLASSify training.

    ``unsstate`` is 0 for supervised learning (default) or 1 for unsupervised.
    The returned ``parameters`` dict can be shown to the user so they can
    pick which models to train and tweak hyperparameters before
    ``classify_start_training_job``.

    Args:
        unsstate: 0 for supervised (default), 1 for unsupervised.
        **kwargs: Tool kwargs (``_credentials``).

    Returns:
        An MCP UI response dict with a parameter Table Card and ``_data``.
    """
    try:
        client = _build_client(kwargs)
        resp = client.get("/reports/get-ml-opts", params={"unsstate": int(unsstate)})
        payload = _safe_json(resp)
        parameters = payload.get("parameters") if isinstance(payload, dict) else None
        if not isinstance(parameters, dict) or not parameters:
            # Fallback: response didn't match the expected shape — show the raw JSON.
            return _ui(
                [Card(
                    title="CLASSify hyperparameter options",
                    content=[Text(content=json.dumps(payload, indent=2)[:4000])],
                )],
                data=payload,
            )

        header_parts = [f"**{len(parameters)} parameter(s) available** (unsstate={int(unsstate)})"]
        if isinstance(payload.get("message"), str) and payload["message"].strip():
            header_parts.append(payload["message"].strip())
        if payload.get("success") is False:
            header_parts.append("_Upstream reported success: false._")
        header = "\n\n".join(header_parts)

        rows = []
        for name, meta in parameters.items():
            if not isinstance(meta, dict):
                rows.append([name, "—", _format_default_value(meta), "—", ""])
                continue
            rows.append([
                name,
                str(meta.get("type", "")),
                _format_default_value(meta.get("default")),
                _format_models_list(meta.get("models")),
                str(meta.get("help", "")),
            ])
        params_table = Table(
            headers=["Parameter", "Type", "Default", "Applies to", "Description"],
            rows=rows,
        )
        return _ui(
            [Card(
                title="CLASSify hyperparameter options",
                content=[Text(content=header), params_table],
            )],
            data=payload,
        )
    except (ExternalHttpError, ValueError) as e:
        return _ui([Alert(message=_user_facing_error(e), variant="error")], retryable=False)


def _make_status_poll(client: "_wrapper.ExternalServiceClient", report_uuid: str):
    """Build a sync callable that probes /reports/get-job-status for one job.

    Normalizes the upstream status string into the JobPoller's vocabulary:
        "Processed"           → succeeded (+ fetches /result/get-results)
        "Processing"          → in_progress
        "N/M Processed"       → in_progress with percentage = N/M
        anything else         → failed

    Args:
        client: A validated CLASSify HTTP client.
        report_uuid: The job's report UUID.

    Returns:
        A zero-arg callable returning the normalized status dict.
    """
    def _poll():
        resp = client.get("/reports/get-job-status", params={"report_uuid": report_uuid})
        payload = _safe_json(resp)
        raw = (payload.get("status") or "").strip()
        if raw == "Processed":
            try:
                results_resp = client.get(
                    "/result/get-results", params={"report_uuid": report_uuid}
                )
                results = _safe_json(results_resp) or results_resp.text
            except Exception:
                results = None
            return {
                "status": "succeeded",
                "percentage": 100,
                "message": "Training complete.",
                "result": results,
            }
        if raw == "Processing":
            return {"status": "in_progress", "percentage": None, "message": raw}
        m = re.match(r"^(\d+)\s*/\s*(\d+)\s+Processed$", raw)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            percentage = int(done * 100 / total) if total else None
            return {"status": "in_progress", "percentage": percentage, "message": raw}
        return {
            "status": "failed",
            "percentage": None,
            "message": raw or "Unknown error",
            "result": None,
        }
    return _poll


def classify_start_training_job(report_uuid: str, class_column: str,
                                models_to_train: Optional[List[str]] = None,
                                parameter_overrides: Optional[Dict[str, Any]] = None,
                                parameter_tune: bool = False,
                                supervised: bool = True,
                                autodetermineclusters: bool = False,
                                unsstate: Optional[int] = None,
                                options: Optional[List[Dict[str, Any]]] = None,
                                **kwargs):
    """Start a CLASSify training job and register the JobPoller.

    Returns immediately with the ``report_uuid`` and ``status: "started"``.
    The agent's JobPoller posts ``tool_progress`` messages into the chat as
    the job runs and a terminal message with metrics on completion.

    Default (auto-build) path mirrors ``submission_example.startJob``:
    internally calls ``/reports/get-ml-opts``, filters ``train_group`` to
    ``models_to_train``, uses upstream defaults for every other parameter
    (forcing ``parameter_tune`` to False unless overridden in
    ``parameter_overrides``), and appends the four script-required entries
    (``report_uuid``, ``class_column``, ``supervised``, ``autodetermineclusters``)
    using string ``"True"`` / ``"False"`` to match the script byte-for-byte.

    ``unsstate`` is auto-derived from ``supervised`` when not specified:
    ``1`` for supervised, ``0`` for unsupervised — matches both the live
    classify.ai.uky.edu deployment and the published API docs. Pass an
    explicit ``unsstate`` only when working around API drift.

    ``parameter_goal`` is special-cased: if the upstream response includes a
    ``parameter_goal`` entry whose meta dict is keyed by the available goal
    names (e.g. ``{'f1_macro': ..., 'precision_macro': ..., ...}``), the
    caller-supplied value from ``parameter_overrides`` is validated against
    those keys, falling back to ``'f1_macro'``.

    Escape hatch: pass a pre-built ``options`` list to skip the internal
    ``get-ml-opts`` call (the four required entries are still appended).

    Args:
        report_uuid: Report UUID from ``classify_submit_dataset``.
        class_column: Name of the class column.
        models_to_train: Models to train (defaults to the script defaults).
        parameter_overrides: Sparse ``{param: value}`` override map.
        parameter_tune: Enable CLASSify's parameter tuning (default False).
        supervised: Default True; False for unsupervised clustering.
        autodetermineclusters: Only meaningful when ``supervised=False``.
        unsstate: Optional get-ml-opts mode override.
        options: Pre-built options list escape hatch.
        **kwargs: Tool kwargs (``_credentials``, ``_runtime``).

    Returns:
        An MCP UI response dict with a started-confirmation Card and ``_data``.
    """
    try:
        client = _build_client(kwargs)
        models_to_train = models_to_train or ["randomforest", "gradientboosting"]
        overrides = parameter_overrides or {}
        if unsstate is None:
            unsstate = 1 if supervised else 0
        if options:
            if not isinstance(options, list):
                raise ValueError("options must be a list of {'name','value'} dicts.")
            args: List[Dict[str, Any]] = list(options)
        else:
            ml_opts_resp = client.get(
                "/reports/get-ml-opts", params={"unsstate": int(unsstate)},
            )
            ml_opts_payload = _safe_json(ml_opts_resp) or {}
            parameters = ml_opts_payload.get("parameters") or {}
            logger.info(
                "CLASSify classify_start_training_job — /get-ml-opts(unsstate=%d) returned %d "
                "parameter(s); train_group default=%r; models_to_train=%r; "
                "supervised=%s",
                int(unsstate), len(parameters),
                (parameters.get("train_group") or {}).get("default") if isinstance(parameters.get("train_group"), dict) else None,
                models_to_train, supervised,
            )
            args = []
            for key, meta in parameters.items():
                meta = meta if isinstance(meta, dict) else {}
                if key == "train_group":
                    for model in (meta.get("default") or []):
                        if model in models_to_train:
                            args.append({"name": key, "value": model})
                elif key == "parameter_goal":
                    requested = overrides.get("parameter_goal", "f1_macro")
                    value = requested if requested in meta else "f1_macro"
                    args.append({"name": key, "value": value})
                else:
                    if key in overrides:
                        value = overrides[key]
                    else:
                        value = meta.get("default")
                        if key == "parameter_tune":
                            value = bool(parameter_tune)
                    args.append({"name": key, "value": value})
            # Defensive: if the upstream /get-ml-opts response didn't yield any
            # train_group entries (response shape drift, model-name mismatch,
            # or empty intersection with models_to_train), seed them directly
            # from models_to_train so the upstream isn't asked to run a job
            # with no models selected.
            if not any(entry.get("name") == "train_group" for entry in args):
                logger.warning(
                    "CLASSify /get-ml-opts produced no usable train_group entries "
                    "(upstream default=%r, requested=%r); falling back to "
                    "models_to_train directly.",
                    (parameters.get("train_group") or {}).get("default") if isinstance(parameters.get("train_group"), dict) else None,
                    models_to_train,
                )
                for model in models_to_train:
                    args.append({"name": "train_group", "value": model})
        args.append({"name": "report_uuid", "value": report_uuid})
        args.append({"name": "class_column", "value": class_column})
        args.append({"name": "supervised", "value": "True" if supervised else "False"})
        args.append({"name": "autodetermineclusters",
                     "value": "True" if autodetermineclusters else "False"})
        logger.info(
            "CLASSify classify_start_training_job — sending %d option(s) for report %s: %s",
            len(args), report_uuid, [(e.get("name"), e.get("value")) for e in args],
        )
        resp = client.post(
            "/reports/start-training-job",
            data={
                "report_uuid": report_uuid,
                "options": json.dumps(args),
            },
        )
        payload = _safe_json(resp)
        runtime = kwargs.get("_runtime")
        if runtime is not None:
            runtime.start_long_running_job(_make_status_poll(client, report_uuid))
        return _ui(
            [Card(
                title="CLASSify training started",
                content=[Text(content=(
                    f"Report UUID: {report_uuid}\n"
                    "Progress will be posted in this chat as the job runs."
                ))],
            )],
            data={
                "report_uuid": report_uuid,
                "status": "started",
                "class_column": class_column,
                "models_to_train": models_to_train,
                "parameter_overrides": overrides,
                "upstream_response": payload,
                "message": "Training started. Progress will appear here automatically.",
            },
        )
    except (ExternalHttpError, ValueError) as e:
        return _ui([Alert(message=_user_facing_error(e), variant="error")], retryable=False)


def _humanize_param_name(name: str) -> str:
    """Turn a snake_case parameter name into a label.

    Args:
        name: The raw upstream parameter name.

    Returns:
        A capitalized, space-separated label.
    """
    if not name:
        return ""
    return name.replace("_", " ").strip().capitalize()


def _classify_field_kind(default: Any) -> str:
    """Pick a ParamPicker field kind based on the upstream default's type.

    Args:
        default: The upstream default value.

    Returns:
        One of ``boolean`` / ``number`` / ``checklist`` / ``text``.
    """
    if isinstance(default, bool):
        return "boolean"
    if isinstance(default, (int, float)):
        return "number"
    if isinstance(default, list):
        return "checklist"
    return "text"


_TRAIN_GROUP_DEFAULTS = ["randomforest", "gradientboosting"]


def propose_training_config(report_uuid: str, class_column: str,
                            supervised: bool = True,
                            autodetermineclusters: bool = False,
                            unsstate: Optional[int] = None,
                            **kwargs):
    """Render an interactive ParamPicker with every hyperparameter from
    ``/reports/get-ml-opts``. Submitting the form posts a chat message that
    asks the LLM to dispatch ``classify_start_training_job`` with the chosen
    values.

    Use this between ``set_column_types`` and ``classify_start_training_job``
    so the user can review and adjust training options in a single form
    instead of answering a stream of clarification questions in chat.

    Args:
        report_uuid: Report UUID from ``classify_submit_dataset``.
        class_column: Name of the class column.
        supervised: Default True; off = unsupervised clustering.
        autodetermineclusters: Only meaningful when ``supervised`` is False.
        unsstate: Optional get-ml-opts mode override.
        **kwargs: Tool kwargs (``_credentials``).

    Returns:
        An MCP UI response dict with a ParamPicker component and ``_data``.
    """
    try:
        client = _build_client(kwargs)
        if unsstate is None:
            unsstate = 1 if supervised else 0
        ml_opts_resp = client.get(
            "/reports/get-ml-opts", params={"unsstate": int(unsstate)},
        )
        ml_opts_payload = _safe_json(ml_opts_resp) or {}
        parameters = ml_opts_payload.get("parameters") or {}
        if not isinstance(parameters, dict) or not parameters:
            return _ui([Alert(
                message=(
                    f"CLASSify returned no hyperparameters for unsstate={int(unsstate)}. "
                    "Cannot build a configuration form."
                ),
                variant="error",
            )], retryable=False)

        fields: List[Dict[str, Any]] = [
            {
                "name": "__supervised__",
                "label": "Supervised learning",
                "kind": "boolean",
                "default": bool(supervised),
                "help": "Off = unsupervised clustering.",
            },
            {
                "name": "__autodetermineclusters__",
                "label": "Auto-determine cluster count",
                "kind": "boolean",
                "default": bool(autodetermineclusters),
                "help": "Only meaningful when supervised is off.",
            },
        ]

        for name, meta in parameters.items():
            meta = meta if isinstance(meta, dict) else {}
            default = meta.get("default")
            help_text = str(meta.get("help") or "")
            entry: Dict[str, Any] = {
                "name": name,
                "label": _humanize_param_name(name),
                "help": help_text,
            }
            if name == "train_group":
                allowed = default if isinstance(default, list) else []
                preselected = [m for m in _TRAIN_GROUP_DEFAULTS if m in allowed]
                if not preselected:
                    preselected = list(allowed[:2])
                entry["kind"] = "checklist"
                entry["options"] = list(allowed)
                entry["default"] = preselected
                entry["label"] = "Models to train"
            elif name == "parameter_goal" and "default" not in meta:
                option_keys = [k for k in meta.keys() if k not in ("help", "type")]
                entry["kind"] = "select"
                entry["options"] = option_keys
                entry["default"] = (
                    "f1_macro" if "f1_macro" in option_keys
                    else (option_keys[0] if option_keys else None)
                )
                entry["label"] = "Parameter goal"
            elif isinstance(default, list):
                entry["kind"] = "checklist"
                entry["options"] = list(default)
                entry["default"] = list(default)
            else:
                entry["kind"] = _classify_field_kind(default)
                entry["default"] = default
                if entry["kind"] == "number" and isinstance(default, float):
                    entry["step"] = 0.01
            fields.append(entry)

        submit_message_template = (
            "Please call classify_start_training_job with the following arguments "
            "(chosen via the parameter picker UI):\n"
            f"- report_uuid: {report_uuid!r}\n"
            f"- class_column: {class_column!r}\n"
            "- models_to_train: {train_group}\n"
            "- supervised: {__supervised__}\n"
            "- autodetermineclusters: {__autodetermineclusters__}\n"
            "- All other settings (every form field except train_group, "
            "__supervised__, __autodetermineclusters__) go into "
            "parameter_overrides as a dict:\n"
            "{__values_json__}\n"
        )

        return _ui(
            [ParamPicker(
                title=f"Configure training for {report_uuid}",
                description=(
                    f"Review the {len(parameters)} hyperparameter(s) CLASSify "
                    f"will use. Adjust any value, then click '{('Start training')}' "
                    "to launch the job. You can also describe changes in chat."
                ),
                fields=fields,
                submit_label="Start training",
                submit_message_template=submit_message_template,
            )],
            data={
                "report_uuid": report_uuid,
                "class_column": class_column,
                "unsstate": int(unsstate),
                "parameter_count": len(parameters),
            },
        )
    except (ExternalHttpError, ValueError) as e:
        return _ui([Alert(message=_user_facing_error(e), variant="error")], retryable=False)


def classify_get_job_status(report_uuid: str, **kwargs):
    """Synchronously probe the status of a CLASSify job by report_uuid.

    Args:
        report_uuid: The job's report UUID.
        **kwargs: Tool kwargs (``_credentials``).

    Returns:
        An MCP UI response dict with a status Card and ``_data``.
    """
    try:
        client = _build_client(kwargs)
        poll = _make_status_poll(client, report_uuid)
        result = poll()
        return _ui(
            [Card(
                title=f"Job {report_uuid}",
                content=[Text(content=(
                    f"Status: {result['status']}\n"
                    f"Message: {result.get('message') or '(none)'}"
                    + (f"\nPercentage: {result['percentage']}%" if result.get("percentage") is not None else "")
                ))],
            )],
            data={"report_uuid": report_uuid, **result},
        )
    except (ExternalHttpError, ValueError) as e:
        return _ui([Alert(message=_user_facing_error(e), variant="error")], retryable=False)


def classify_get_results(report_uuid: str, **kwargs):
    """Fetch the final performance metrics for a completed CLASSify job.

    Shape-detected rendering:
      * ``{model: {metric: value, ...}, ...}``  → one row per model, columns = metric names
      * ``{metric: value, ...}`` (flat)         → two-column Metric | Value table
      * anything else (text, mixed nesting)     → fall back to a truncated JSON Text block

    Args:
        report_uuid: The job's report UUID.
        **kwargs: Tool kwargs (``_credentials``).

    Returns:
        An MCP UI response dict with a results Card and ``_data``.
    """
    try:
        client = _build_client(kwargs)
        resp = client.get("/result/get-results", params={"report_uuid": report_uuid})
        payload = _safe_json(resp)
        data = {"report_uuid": report_uuid, "results": payload or (resp.text if resp.content else "")}

        if isinstance(payload, dict) and payload:
            if all(isinstance(v, dict) for v in payload.values()):
                # Per-model metrics shape.
                metric_keys: List[str] = []
                seen = set()
                for model_metrics in payload.values():
                    for k in model_metrics.keys():
                        if k not in seen:
                            seen.add(k)
                            metric_keys.append(k)
                metric_keys.sort()
                rows = [
                    [model] + [_render_metric_value(metrics.get(k)) for k in metric_keys]
                    for model, metrics in payload.items()
                ]
                table = Table(headers=["Model"] + metric_keys, rows=rows)
                return _ui(
                    [Card(
                        title=f"Results for {report_uuid}",
                        content=[
                            Text(content=f"**{len(payload)} model(s)**, **{len(metric_keys)} metric(s)**."),
                            table,
                        ],
                    )],
                    data=data,
                )
            if all(not isinstance(v, dict) for v in payload.values()):
                # Flat metrics shape.
                rows = [[k, _render_metric_value(v)] for k, v in payload.items()]
                table = Table(headers=["Metric", "Value"], rows=rows)
                return _ui(
                    [Card(
                        title=f"Results for {report_uuid}",
                        content=[table],
                    )],
                    data=data,
                )

        # Fallback: not a dict, mixed nesting, or non-JSON body.
        body = (
            json.dumps(payload, indent=2)[:4000]
            if payload
            else (resp.text[:4000] if resp.content else "(empty)")
        )
        return _ui(
            [Card(
                title=f"Results for {report_uuid}",
                content=[Text(content=body)],
            )],
            data=data,
        )
    except (ExternalHttpError, ValueError) as e:
        return _ui([Alert(message=_user_facing_error(e), variant="error")], retryable=False)


def get_output_log(report_uuid: str, **kwargs):
    """Fetch the output log for a CLASSify job (most useful when a job failed).

    Args:
        report_uuid: The job's report UUID.
        **kwargs: Tool kwargs (``_credentials``).

    Returns:
        An MCP UI response dict with a truncated log Card and ``_data``.
    """
    try:
        client = _build_client(kwargs)
        resp = client.get("/result/get-output-log", params={"report_uuid": report_uuid})
        # Log is generally plain text; fall back to a JSON dump if it's not.
        text = resp.text if resp.content else ""
        if len(text) > 4000:
            text = text[:4000] + "\n… (truncated)"
        return _ui(
            [Card(
                title=f"Output log for {report_uuid}",
                content=[Text(content=text or "(empty)")],
            )],
            data={"report_uuid": report_uuid, "log": resp.text if resp.content else ""},
        )
    except (ExternalHttpError, ValueError) as e:
        return _ui([Alert(message=_user_facing_error(e), variant="error")], retryable=False)


def classify_delete_dataset(report_uuid: str, **kwargs):
    """Delete a CLASSify dataset and all of its associated models / files.

    Args:
        report_uuid: The report UUID to delete.
        **kwargs: Tool kwargs (``_credentials``).

    Returns:
        An MCP UI response dict with a confirmation Card and ``_data``.
    """
    try:
        client = _build_client(kwargs)
        resp = client.post("/reports/delete", data={"report_uuid": report_uuid})
        _forget_report_attachment(report_uuid)
        payload = _safe_json(resp)
        return _ui(
            [Card(
                title="Dataset deleted",
                content=[Text(content=f"Report {report_uuid} has been removed from CLASSify.")],
            )],
            data={"report_uuid": report_uuid, "response": payload},
        )
    except (ExternalHttpError, ValueError) as e:
        return _ui([Alert(message=_user_facing_error(e), variant="error")], retryable=False)


# ---------------------------------------------------------------------------
# Tool registry (CLASSify slice — merged into the union by mcp_tools)
# ---------------------------------------------------------------------------


_COLUMN_CHANGE_ITEM_DESCRIPTION = (
    "Per-column config. Fields: 'column' (str, required), "
    "'data_type' (str: 'integer'|'float'|'bool'|'string', required), "
    "'checked' (bool, include in final dataset, required), "
    "'missing' (null|'synthetic'|'constant', how to handle missing cells), "
    "'fill_value' (any, used when missing='constant'), "
    "'class' (bool, set True on exactly one entry to mark the class column)."
)


TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "classify_submit_dataset": {
        "function": classify_submit_dataset,
        "description": (
            "Upload a CSV dataset to CLASSify. Returns a report_uuid and the inferred "
            "column data types. Use the returned types to converse with the user about "
            "which column is the class, which to drop, and how to fill missing values "
            "before calling set_column_types. "
            "DO NOT call read_spreadsheet, read_csv, or any other file-reading tool "
            "before this — classify_submit_dataset returns the column names and inferred "
            "types directly, and set_column_types handles missing-value detection "
            "internally via pandas. This is the FIRST step of the CLASSify pipeline; "
            "call it as soon as the user provides a file_handle. file_handle should be "
            "the attachment_id the upload mechanism gave you, NOT the display filename. "
            "If the user pasted data in chat, pass it via inline_data — NEVER invent "
            "a file_handle. "
            "After this call, set_column_types only needs the report_uuid — the agent "
            "remembers the file location automatically."
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
    "set_column_types": {
        "function": set_column_types,
        "description": (
            "Build and apply per-column data-type / missing-value / class-column choices to "
            "a submitted dataset. Re-reads the CSV via pandas to detect which columns have "
            "missing values (using the local path stashed during classify_submit_dataset), "
            "then defaults missing handling to 'synthetic' to mirror CLASSify's recommended "
            "pipeline (submission_example.py). Pass column_types from "
            "classify_submit_dataset's response. Do NOT pass file_handle — the agent "
            "remembers it from classify_submit_dataset automatically. For advanced "
            "overrides, pass a hand-built column_changes list to bypass automatic building."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "report_uuid": {
                    "type": "string",
                    "description": "Report UUID returned by submit_dataset.",
                },
                "class_column": {
                    "type": "string",
                    "description": "Name of the class column.",
                },
                "column_types": {
                    "type": "object",
                    "description": (
                        "data_types map from submit_dataset._data.column_types "
                        "(e.g. {'col_a': 'integer', 'col_b': 'string'})."
                    ),
                    "additionalProperties": {"type": "string"},
                },
                "missing_strategy": {
                    "type": "string",
                    "enum": ["synthetic", "constant", "none"],
                    "default": "synthetic",
                    "description": (
                        "How to handle missing cells in columns that contain nulls. "
                        "'synthetic' is the script's default."
                    ),
                },
                "fill_value": {
                    "description": "Used only when missing_strategy='constant'.",
                },
                "excluded_columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Columns to mark checked=False (drop from training).",
                },
                "column_changes": {
                    "type": "array",
                    "description": (
                        "Optional escape hatch: a hand-built per-column list overrides "
                        "automatic building. " + _COLUMN_CHANGE_ITEM_DESCRIPTION
                    ),
                    "items": {"type": "object"},
                },
            },
            "required": ["report_uuid"],
        },
        "scope": "tools:write",
    },
    "get_ml_options": {
        "function": get_ml_options,
        "description": (
            "Return the parameters dict the user can override before training. "
            "unsstate=0 for supervised learning (default), 1 for unsupervised."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "unsstate": {
                    "type": "integer",
                    "description": "0 for supervised (default), 1 for unsupervised.",
                    "default": 0,
                },
            },
            "additionalProperties": False,
        },
        "scope": "tools:read",
    },
    "classify_start_training_job": {
        "function": classify_start_training_job,
        "description": (
            "Kick off CLASSify training on a previously-configured dataset. Internally "
            "fetches /reports/get-ml-opts, filters 'train_group' to models_to_train, uses "
            "upstream defaults for every other parameter (with parameter_tune forced to "
            "False unless overridden), and appends the four script-required entries "
            "(report_uuid, class_column, supervised, autodetermineclusters). Returns the "
            "report_uuid immediately and posts progress + final results into the chat "
            "automatically as the job runs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "report_uuid": {"type": "string", "description": "Report UUID from classify_submit_dataset."},
                "class_column": {"type": "string", "description": "Name of the class column."},
                "models_to_train": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": ["randomforest", "gradientboosting"],
                    "description": (
                        "Models to train; entries that appear in the upstream 'train_group' "
                        "default list are emitted as separate options entries. Defaults to "
                        "['randomforest', 'gradientboosting'] (script's default). "
                        "Supervised models: randomforest, gradientboosting, xgboost, "
                        "histgradientboosting, bagging, neuralnetwork, tabpfn, sgdclassifier, "
                        "logisticregression, kneighbors. Unsupervised: spectralclustering, "
                        "hdbscan, kmeans."
                    ),
                },
                "parameter_overrides": {
                    "type": "object",
                    "description": (
                        "Sparse {param_name: value} map; missing keys use upstream defaults."
                    ),
                    "additionalProperties": True,
                },
                "parameter_tune": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Whether to enable CLASSify's parameter tuning. Default False to "
                        "mirror the script."
                    ),
                },
                "supervised": {
                    "type": "boolean",
                    "description": "Default True. Set False for unsupervised clustering.",
                    "default": True,
                },
                "autodetermineclusters": {
                    "type": "boolean",
                    "description": "Only meaningful when supervised=False.",
                    "default": False,
                },
                "unsstate": {
                    "type": "integer",
                    "description": (
                        "Optional get-ml-opts mode override. Auto-derived from "
                        "'supervised' by default (1 for supervised, 0 for unsupervised) "
                        "to match the live CLASSify deployment. Only set this if the "
                        "deployment's API has drifted again."
                    ),
                },
            },
            "required": ["report_uuid", "class_column"],
            "additionalProperties": False,
        },
        "scope": "tools:write",
    },
    "propose_training_config": {
        "function": propose_training_config,
        "description": (
            "Render an interactive parameter-picker form so the user can review and "
            "adjust every CLASSify hyperparameter (from /reports/get-ml-opts) before "
            "training. Submitting the form (Start training button) emits a chat "
            "message that triggers classify_start_training_job with the user's choices. "
            "Call this AFTER set_column_types and BEFORE classify_start_training_job — "
            "it lets the user pick models and tune hyperparameters interactively in one "
            "step instead of via a long Q&A in chat."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "report_uuid": {"type": "string", "description": "Report UUID from submit_dataset."},
                "class_column": {"type": "string", "description": "Name of the class column."},
                "supervised": {
                    "type": "boolean",
                    "description": "Default True. Off = unsupervised clustering.",
                    "default": True,
                },
                "autodetermineclusters": {
                    "type": "boolean",
                    "description": "Only meaningful when supervised is False.",
                    "default": False,
                },
                "unsstate": {
                    "type": "integer",
                    "description": (
                        "Optional get-ml-opts mode override; auto-derived from supervised."
                    ),
                },
            },
            "required": ["report_uuid", "class_column"],
            "additionalProperties": False,
        },
        "scope": "tools:read",
    },
    "classify_get_job_status": {
        "function": classify_get_job_status,
        "description": (
            "Synchronously probe the status of a CLASSify job by report_uuid. The poller "
            "usually pushes updates automatically; use this only for explicit user "
            "'did my job finish?' queries."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"report_uuid": {"type": "string"}},
            "required": ["report_uuid"],
        },
        "scope": "tools:read",
    },
    "classify_get_results": {
        "function": classify_get_results,
        "description": "Fetch the final performance metrics for a completed CLASSify job.",
        "input_schema": {
            "type": "object",
            "properties": {"report_uuid": {"type": "string"}},
            "required": ["report_uuid"],
        },
        "scope": "tools:read",
    },
    "get_output_log": {
        "function": get_output_log,
        "description": "Fetch the output log for a CLASSify job (helpful when a job failed).",
        "input_schema": {
            "type": "object",
            "properties": {"report_uuid": {"type": "string"}},
            "required": ["report_uuid"],
        },
        "scope": "tools:read",
    },
    "classify_delete_dataset": {
        "function": classify_delete_dataset,
        "description": "Delete a CLASSify dataset and all of its associated models / files.",
        "input_schema": {
            "type": "object",
            "properties": {"report_uuid": {"type": "string"}},
            "required": ["report_uuid"],
        },
        "scope": "tools:write",
        "metadata": {"external_target": "CLASSify"},
    },
}
