"""Opt-in end-to-end smoke tests for the ML Services agent against live CLASSify,
Forecaster, and LLM-Factory deployments; skipped unless the matching *_E2E_URL
environment variables are set.
"""

import os
import time
from pathlib import Path

import pytest

from agents.ml_services import classify_tools, forecaster_tools, llm_factory_tools

CLASSIFY_E2E_URL = os.getenv("CLASSIFY_E2E_URL")
CLASSIFY_E2E_KEY = os.getenv("CLASSIFY_E2E_API_KEY")

_CLASSIFY_CSV_CANDIDATES = [
    os.getenv("CLASSIFY_E2E_CSV"),
    "/app/dataset_soft.csv",
    str(Path(__file__).resolve().parents[3] / "dataset_soft.csv"),
]


def _resolve_csv_path(candidates) -> str:
    for cand in candidates:
        if cand and Path(cand).exists():
            return cand
    return ""


CLASSIFY_CSV_PATH = _resolve_csv_path(_CLASSIFY_CSV_CANDIDATES)


@pytest.mark.skipif(
    not CLASSIFY_E2E_URL or not CLASSIFY_E2E_KEY,
    reason="CLASSIFY_E2E_URL and CLASSIFY_E2E_API_KEY env vars must be set",
)
def test_classify_credentials_check_against_live_service() -> None:
    creds = {"CLASSIFY_URL": CLASSIFY_E2E_URL, "CLASSIFY_API_KEY": CLASSIFY_E2E_KEY}
    result = classify_tools._credentials_check(_credentials=creds)
    assert result["credential_test"] == "ok", (
        f"Live CLASSify probe did not return 'ok': {result}"
    )


@pytest.mark.skipif(
    not CLASSIFY_E2E_URL or not CLASSIFY_E2E_KEY,
    reason="CLASSIFY_E2E_URL and CLASSIFY_E2E_API_KEY env vars must be set",
)
def test_classify_get_ml_options_returns_payload() -> None:
    creds = {"CLASSIFY_URL": CLASSIFY_E2E_URL, "CLASSIFY_API_KEY": CLASSIFY_E2E_KEY}
    result = classify_tools.get_ml_options(_credentials=creds)
    assert "_data" in result
    assert result["_ui_components"][0].get("variant") != "error"


@pytest.mark.skipif(
    not CLASSIFY_E2E_URL or not CLASSIFY_E2E_KEY or not CLASSIFY_CSV_PATH,
    reason=(
        "CLASSIFY_E2E_URL, CLASSIFY_E2E_API_KEY, and a CSV at "
        "CLASSIFY_E2E_CSV (or /app/dataset_soft.csv) must be set."
    ),
)
def test_classify_full_pipeline_against_live_service() -> None:
    creds = {"CLASSIFY_URL": CLASSIFY_E2E_URL, "CLASSIFY_API_KEY": CLASSIFY_E2E_KEY}
    report_uuid = None
    try:
        submit = classify_tools.classify_submit_dataset(
            file_handle=CLASSIFY_CSV_PATH, _credentials=creds, user_id="e2e-smoke",
        )
        assert submit["_ui_components"][0].get("variant") != "error", (
            f"classify_submit_dataset failed: {submit}"
        )
        report_uuid = submit["_data"]["report_uuid"]
        column_types = submit["_data"]["column_types"] or {}
        assert report_uuid, "classify_submit_dataset returned no report_uuid"
        assert column_types, "classify_submit_dataset returned no column_types"
        print(
            f"[E2E] classify_submit_dataset → report_uuid={report_uuid} "
            f"columns={len(column_types)}"
        )

        class_column = "class" if "class" in column_types else list(column_types)[-1]
        set_cols = classify_tools.set_column_types(
            report_uuid=report_uuid,
            class_column=class_column,
            column_types=column_types,
            _credentials=creds,
            user_id="e2e-smoke",
        )
        assert set_cols["_ui_components"][0].get("variant") != "error", (
            f"set_column_types failed: {set_cols}"
        )
        print(f"[E2E] set_column_types → class_column={class_column}")

        start = classify_tools.classify_start_training_job(
            report_uuid=report_uuid,
            class_column=class_column,
            models_to_train=["randomforest"],
            parameter_tune=False,
            supervised=True,
            _credentials=creds,
        )
        assert start["_ui_components"][0].get("variant") != "error", (
            f"classify_start_training_job failed: {start}"
        )
        print("[E2E] classify_start_training_job → started")

        client = classify_tools.make_client(creds)
        poll = classify_tools._make_status_poll(client, report_uuid)
        deadline = time.time() + 600
        terminal = None
        while time.time() < deadline:
            res = poll()
            print(f"[E2E] poll → status={res['status']} message={res.get('message')!r}")
            if res["status"] in ("succeeded", "failed"):
                terminal = res
                break
            time.sleep(5)
        assert terminal is not None, "Training did not terminate within 10 minutes"
        assert terminal["status"] == "succeeded", (
            f"Training reported terminal status {terminal}"
        )

        results = classify_tools.classify_get_results(
            report_uuid=report_uuid, _credentials=creds,
        )
        assert results["_ui_components"][0].get("variant") != "error", (
            f"classify_get_results failed: {results}"
        )
        payload = results["_data"]["results"]
        print(
            f"[E2E] classify_get_results → results type={type(payload).__name__} "
            f"keys={list(payload) if isinstance(payload, dict) else 'n/a'}"
        )
        assert payload, "classify_get_results returned no payload"
    finally:
        if report_uuid:
            try:
                deleted = classify_tools.classify_delete_dataset(
                    report_uuid=report_uuid, _credentials=creds,
                )
                print(f"[E2E] classify_delete_dataset → {deleted['_data'].get('response')}")
            except Exception as cleanup_err:
                print(f"[E2E] classify_delete_dataset cleanup failed: {cleanup_err}")


FORECASTER_E2E_URL = os.getenv("FORECASTER_E2E_URL")
FORECASTER_E2E_KEY = os.getenv("FORECASTER_E2E_API_KEY")

_FORECASTER_CSV_CANDIDATES = [
    os.getenv("FORECASTER_E2E_CSV"),
    "/app/bikerides_day.csv",
    str(Path(__file__).resolve().parents[3] / "bikerides_day.csv"),
]

FORECASTER_CSV_PATH = _resolve_csv_path(_FORECASTER_CSV_CANDIDATES)


@pytest.mark.skipif(
    not FORECASTER_E2E_URL or not FORECASTER_E2E_KEY,
    reason="FORECASTER_E2E_URL and FORECASTER_E2E_API_KEY env vars must be set",
)
def test_forecaster_credentials_check_against_live_service() -> None:
    creds = {"FORECASTER_URL": FORECASTER_E2E_URL, "FORECASTER_API_KEY": FORECASTER_E2E_KEY}
    result = forecaster_tools._credentials_check(_credentials=creds)
    assert result["credential_test"] == "ok", (
        f"Live Forecaster probe did not return 'ok': {result}"
    )


@pytest.mark.skipif(
    not FORECASTER_E2E_URL or not FORECASTER_E2E_KEY or not FORECASTER_CSV_PATH,
    reason=(
        "FORECASTER_E2E_URL, FORECASTER_E2E_API_KEY, and a CSV at "
        "FORECASTER_E2E_CSV (or /app/bikerides_day.csv) must be set."
    ),
)
def test_forecaster_full_pipeline_against_live_service() -> None:
    creds = {"FORECASTER_URL": FORECASTER_E2E_URL, "FORECASTER_API_KEY": FORECASTER_E2E_KEY}
    uuid = None
    try:
        submit = forecaster_tools.forecaster_submit_dataset(
            file_handle=FORECASTER_CSV_PATH, _credentials=creds, user_id="e2e-smoke",
        )
        assert submit["_ui_components"][0].get("variant") != "error", (
            f"forecaster_submit_dataset failed: {submit}"
        )
        uuid = submit["_data"]["uuid"]
        cols = submit["_data"]["columns"]
        assert uuid, "forecaster_submit_dataset returned no uuid"
        assert cols, "forecaster_submit_dataset returned no columns"
        print(f"[E2E] forecaster_submit_dataset → uuid={uuid} columns={cols}")

        column_roles = {
            "Date": "time-component",
            "Volume": "target",
            "Rain": "past-covariates",
            "Temp": "past-covariates",
        }
        if not all(c in cols for c in column_roles):
            column_roles = {cols[0]: "time-component", cols[1]: "target"}
            for extra in cols[2:]:
                column_roles[extra] = "past-covariates"
        save = forecaster_tools.set_column_roles(
            uuid=uuid, column_roles=column_roles, _credentials=creds,
        )
        assert save["_ui_components"][0].get("variant") != "error", (
            f"set_column_roles failed: {save}"
        )
        print(f"[E2E] set_column_roles → {column_roles}")

        options = {
            "models": ["linear-regression"],
            "expanding-window": False,
            "test-size": 0.2,
            "epochs": 1,
            "visualize": False,
        }
        start = forecaster_tools.forecaster_start_training_job(
            uuid=uuid, options=options, _credentials=creds,
        )
        assert start["_ui_components"][0].get("variant") != "error", (
            f"forecaster_start_training_job failed: {start}"
        )
        print(f"[E2E] forecaster_start_training_job → started (options={options})")

        client = forecaster_tools.make_client(creds)
        poll = forecaster_tools._make_status_poll(client, uuid)
        deadline = time.time() + 300
        terminal = None
        while time.time() < deadline:
            res = poll()
            print(f"[E2E] poll → status={res['status']} message={res.get('message')!r}")
            if res["status"] in ("succeeded", "failed"):
                terminal = res
                break
            time.sleep(5)
        assert terminal is not None, "Training did not terminate within 5 minutes"
        assert terminal["status"] == "succeeded", (
            f"Training reported terminal status {terminal}"
        )

        results = forecaster_tools.forecaster_get_results(uuid=uuid, _credentials=creds)
        assert results["_ui_components"][0].get("variant") != "error", (
            f"forecaster_get_results failed: {results}"
        )
        metrics = results["_data"]["metrics"]
        print(f"[E2E] forecaster_get_results → metrics keys={list(metrics) if isinstance(metrics, dict) else type(metrics).__name__}")
        assert metrics, "forecaster_get_results returned no metrics"
    finally:
        if uuid:
            try:
                deleted = forecaster_tools.forecaster_delete_dataset(
                    uuid=uuid, _credentials=creds,
                )
                print(f"[E2E] forecaster_delete_dataset → {deleted['_data'].get('response')}")
            except Exception as cleanup_err:
                print(f"[E2E] forecaster_delete_dataset cleanup failed: {cleanup_err}")


LLM_FACTORY_E2E_URL = os.getenv("LLM_FACTORY_E2E_URL")
LLM_FACTORY_E2E_KEY = os.getenv("LLM_FACTORY_E2E_API_KEY")


@pytest.mark.skipif(
    not LLM_FACTORY_E2E_URL or not LLM_FACTORY_E2E_KEY,
    reason="LLM_FACTORY_E2E_URL and LLM_FACTORY_E2E_API_KEY env vars must be set",
)
def test_llm_factory_credentials_check_against_live_service() -> None:
    creds = {"LLM_FACTORY_URL": LLM_FACTORY_E2E_URL, "LLM_FACTORY_API_KEY": LLM_FACTORY_E2E_KEY}
    result = llm_factory_tools._credentials_check(_credentials=creds)
    assert result["credential_test"] == "ok", (
        f"Live LLM-Factory probe did not return 'ok': {result}"
    )


@pytest.mark.skipif(
    not LLM_FACTORY_E2E_URL or not LLM_FACTORY_E2E_KEY,
    reason="LLM_FACTORY_E2E_URL and LLM_FACTORY_E2E_API_KEY env vars must be set",
)
def test_llm_factory_list_models_returns_payload() -> None:
    creds = {"LLM_FACTORY_URL": LLM_FACTORY_E2E_URL, "LLM_FACTORY_API_KEY": LLM_FACTORY_E2E_KEY}
    result = llm_factory_tools.list_models(_credentials=creds)
    assert "_data" in result
    assert "models" in result["_data"]
