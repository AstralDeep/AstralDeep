"""Tests for ml_services/forecaster_tools.py: credential checks, dataset submission
(file-handle and inline-data paths), column-role assignment, training-job
start/poll/results, and dataset deletion.
"""

import json
import socket
from unittest.mock import patch

import pytest

from agents.ml_services import forecaster_tools as mcp_tools
from shared.tests._http_mock import HttpMock


SAFE_HOST = "forecaster.example.com"
BASE_URL = f"https://{SAFE_HOST}"
GOOD_CREDS = {"FORECASTER_URL": BASE_URL, "FORECASTER_API_KEY": "sentinel-api-key"}

SUBMIT_URL = f"{BASE_URL}/dataset/submit"
SAVE_COLS_URL = f"{BASE_URL}/dataset/save-columns"
START_JOB_URL = f"{BASE_URL}/dataset/start-training-job"
JOB_STATUS_URL = f"{BASE_URL}/dataset/get-job-status"
RESULTS_URL = f"{BASE_URL}/results/get-metrics"
DELETE_URL = f"{BASE_URL}/dataset/delete"


@pytest.fixture
def rmock():
    with HttpMock() as m:
        yield m


@pytest.fixture(autouse=True)
def stub_dns():
    def _fake(host, *_a, **_kw):
        if host == SAFE_HOST:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]
        raise socket.gaierror(host)
    with patch("socket.getaddrinfo", _fake):
        yield


def test_credentials_check_ok_on_200(rmock: HttpMock) -> None:
    rmock.add("GET", JOB_STATUS_URL, status=200,
              json={"success": False, "message": "A UUID must be provded"})
    result = mcp_tools._credentials_check(_credentials=GOOD_CREDS)
    assert result == {"credential_test": "ok"}
    assert rmock.calls[-1]["url"] == JOB_STATUS_URL
    assert not rmock.calls[-1].get("params")


def test_credentials_check_ok_on_4xx_non_auth(rmock: HttpMock) -> None:
    rmock.add("GET", JOB_STATUS_URL, status=404, json={"detail": "route not found"})
    result = mcp_tools._credentials_check(_credentials=GOOD_CREDS)
    assert result == {"credential_test": "ok"}


def test_credentials_check_auth_failed_401(rmock: HttpMock) -> None:
    rmock.add("GET", JOB_STATUS_URL, status=401, body=b"{}")
    result = mcp_tools._credentials_check(_credentials=GOOD_CREDS)
    assert result["credential_test"] == "auth_failed"


def test_credentials_check_auth_failed_403(rmock: HttpMock) -> None:
    rmock.add("GET", JOB_STATUS_URL, status=403, body=b"{}")
    result = mcp_tools._credentials_check(_credentials=GOOD_CREDS)
    assert result["credential_test"] == "auth_failed"


def test_credentials_check_unreachable() -> None:
    import requests
    with patch("requests.request", side_effect=requests.ConnectionError("nope")):
        result = mcp_tools._credentials_check(_credentials=GOOD_CREDS)
        assert result["credential_test"] == "unreachable"


def test_credentials_check_missing_creds() -> None:
    result = mcp_tools._credentials_check()
    assert result["credential_test"] == "unexpected"
    assert "not configured" in result["detail"].lower()


def test_credentials_check_partial_creds() -> None:
    result = mcp_tools._credentials_check(_credentials={"FORECASTER_URL": BASE_URL})
    assert result["credential_test"] == "unexpected"


def test_no_api_key_in_response_data(rmock: HttpMock) -> None:
    rmock.add("GET", JOB_STATUS_URL, status=200, json={"status": "Unknown"})
    result = mcp_tools._credentials_check(_credentials=GOOD_CREDS)
    assert "sentinel-api-key" not in json.dumps(result)


def test_submit_dataset_returns_uuid_and_columns(rmock: HttpMock, tmp_path) -> None:
    csv = tmp_path / "rides.csv"
    csv.write_text("Date,Volume,Rain,Temp\n2026-01-01,100,0,5\n")
    rmock.add("POST", SUBMIT_URL, status=200, json={
        "uuid": "ds-42",
        "columns": ["Date", "Volume", "Rain", "Temp"],
    })
    result = mcp_tools.forecaster_submit_dataset(
        file_handle=str(csv), _credentials=GOOD_CREDS, user_id="alice",
    )
    assert result["_data"]["uuid"] == "ds-42"
    assert result["_data"]["columns"] == ["Date", "Volume", "Rain", "Temp"]
    assert "not-included" in result["_data"]["allowed_roles"]
    call = rmock.calls[-1]
    assert call["url"] == SUBMIT_URL
    assert "file" in (call.get("files") or {})


def test_submit_dataset_missing_user_id_returns_error(rmock: HttpMock, tmp_path) -> None:
    csv = tmp_path / "data.csv"
    csv.write_text("Date,Value\n2026-01-01,1\n")
    result = mcp_tools.forecaster_submit_dataset(file_handle=str(csv), _credentials=GOOD_CREDS)
    assert result["_ui_components"][0]["variant"] == "error"


def test_submit_dataset_handles_empty_columns(rmock: HttpMock, tmp_path) -> None:
    csv = tmp_path / "data.csv"
    csv.write_text("Date,Value\n2026-01-01,1\n")
    rmock.add("POST", SUBMIT_URL, status=200, json={"uuid": "ds-empty"})
    result = mcp_tools.forecaster_submit_dataset(
        file_handle=str(csv), _credentials=GOOD_CREDS, user_id="alice",
    )
    assert result["_data"]["uuid"] == "ds-empty"
    assert result["_data"]["columns"] == []


def test_submit_dataset_auth_failure_renders_alert(rmock: HttpMock, tmp_path) -> None:
    csv = tmp_path / "data.csv"
    csv.write_text("Date,Value\n2026-01-01,1\n")
    rmock.add("POST", SUBMIT_URL, status=401, body=b"{}")
    result = mcp_tools.forecaster_submit_dataset(
        file_handle=str(csv), _credentials=GOOD_CREDS, user_id="alice",
    )
    assert result["_ui_components"][0]["variant"] == "error"
    assert "rejected" in result["_ui_components"][0]["message"].lower()


INLINE_CSV = "Week,Enrollment\n1,40\n2,42\n3,45\n"


def test_submit_dataset_inline_data_materializes_and_uploads(rmock: HttpMock, tmp_path) -> None:
    materialized = tmp_path / "inline.csv"

    def fake_materialize(text, user_id, *, extension="csv"):
        assert user_id == "alice"
        assert "Week,Enrollment" in text
        assert extension == "csv"
        materialized.write_text(text)
        return str(materialized)

    rmock.add("POST", SUBMIT_URL, status=200, json={
        "uuid": "ds-inline", "columns": ["Week", "Enrollment"],
    })
    with patch.object(mcp_tools, "materialize_text_attachment",
                      side_effect=fake_materialize) as mock_mat:
        result = mcp_tools.forecaster_submit_dataset(
            inline_data=INLINE_CSV, _credentials=GOOD_CREDS, user_id="alice",
        )
    mock_mat.assert_called_once()
    assert result["_data"]["uuid"] == "ds-inline"
    assert result["_data"]["columns"] == ["Week", "Enrollment"]
    call = rmock.calls[-1]
    assert call["url"] == SUBMIT_URL
    assert "file" in (call.get("files") or {})


def test_submit_dataset_inline_data_requires_user_id(rmock: HttpMock) -> None:
    with patch.object(mcp_tools, "materialize_text_attachment") as mock_mat:
        result = mcp_tools.forecaster_submit_dataset(
            inline_data=INLINE_CSV, _credentials=GOOD_CREDS,
        )
    mock_mat.assert_not_called()
    assert result["_ui_components"][0]["variant"] == "error"


def test_submit_dataset_requires_handle_or_inline(rmock: HttpMock) -> None:
    result = mcp_tools.forecaster_submit_dataset(
        _credentials=GOOD_CREDS, user_id="alice",
    )
    assert result["_ui_components"][0]["variant"] == "error"
    assert "inline_data" in result["_ui_components"][0]["message"]


def test_submit_dataset_file_handle_wins_over_inline(rmock: HttpMock, tmp_path) -> None:
    csv = tmp_path / "real.csv"
    csv.write_text("Date,Value\n2026-01-01,1\n")
    rmock.add("POST", SUBMIT_URL, status=200, json={"uuid": "ds-1", "columns": ["Date", "Value"]})
    with patch.object(mcp_tools, "materialize_text_attachment") as mock_mat:
        result = mcp_tools.forecaster_submit_dataset(
            file_handle=str(csv), inline_data=INLINE_CSV,
            _credentials=GOOD_CREDS, user_id="alice",
        )
    mock_mat.assert_not_called()
    assert result["_data"]["uuid"] == "ds-1"


def test_submit_dataset_inline_validation_error_renders_alert(rmock: HttpMock) -> None:
    with patch.object(
        mcp_tools, "materialize_text_attachment",
        side_effect=ValueError("inline_data is not valid CSV: no header row detected."),
    ):
        result = mcp_tools.forecaster_submit_dataset(
            inline_data="not a csv", _credentials=GOOD_CREDS, user_id="alice",
        )
    assert result["_ui_components"][0]["variant"] == "error"
    assert "not valid csv" in result["_ui_components"][0]["message"].lower()


def test_submit_dataset_schema_offers_inline_data() -> None:
    entry = mcp_tools.TOOL_REGISTRY["forecaster_submit_dataset"]
    props = entry["input_schema"]["properties"]
    assert "inline_data" in props
    assert "file_handle" in props
    assert entry["input_schema"]["required"] == []
    assert "NEVER invent" in entry["description"]


def test_set_column_roles_builds_categorized_string(rmock: HttpMock) -> None:
    rmock.add("POST", SAVE_COLS_URL, status=200, json={"ok": True})
    column_roles = {
        "Date": "time-component",
        "Volume": "target",
        "Rain": "past-covariates",
        "Temp": "past-covariates",
    }
    result = mcp_tools.set_column_roles(
        uuid="ds-42", column_roles=column_roles, _credentials=GOOD_CREDS,
    )
    assert result["_ui_components"][0].get("variant") != "error"
    call = rmock.calls[-1]
    assert call["url"] == SAVE_COLS_URL
    sent = call.get("data") or {}
    assert sent.get("uuid") == "ds-42"
    parsed = json.loads(sent["categorizedString"])
    assert parsed["time-component"] == ["Date"]
    assert parsed["target"] == ["Volume"]
    assert sorted(parsed["past-covariates"]) == ["Rain", "Temp"]
    for role in mcp_tools.COLUMN_ROLES:
        assert role in parsed


def test_set_column_roles_rejects_unknown_role() -> None:
    result = mcp_tools.set_column_roles(
        uuid="ds-42",
        column_roles={"Date": "time-component", "Volume": "totally-made-up"},
        _credentials=GOOD_CREDS,
    )
    assert result["_ui_components"][0]["variant"] == "error"
    assert "totally-made-up" in result["_ui_components"][0]["message"]


def test_set_column_roles_rejects_empty_dict() -> None:
    result = mcp_tools.set_column_roles(
        uuid="ds-42", column_roles={}, _credentials=GOOD_CREDS,
    )
    assert result["_ui_components"][0]["variant"] == "error"


def test_start_training_job_posts_form_encoded_options(rmock: HttpMock) -> None:
    rmock.add("POST", START_JOB_URL, status=200, json={"started": True})
    overrides = {"models": ["linear-regression"], "epochs": 1, "expanding-window": False}
    result = mcp_tools.forecaster_start_training_job(
        uuid="ds-42", options=overrides, _credentials=GOOD_CREDS,
    )
    assert result["_data"]["uuid"] == "ds-42"
    assert result["_data"]["status"] == "started"
    call = rmock.calls[-1]
    assert call["url"] == START_JOB_URL
    sent = call.get("data") or {}
    assert sent.get("uuid") == "ds-42"
    parsed = json.loads(sent["options"])
    assert parsed == overrides


def test_start_training_job_with_no_options_sends_empty_dict(rmock: HttpMock) -> None:
    rmock.add("POST", START_JOB_URL, status=200, json={"started": True})
    mcp_tools.forecaster_start_training_job(uuid="ds-42", _credentials=GOOD_CREDS)
    sent = rmock.calls[-1].get("data") or {}
    assert json.loads(sent["options"]) == {}


def test_start_training_job_rejects_non_dict_options() -> None:
    result = mcp_tools.forecaster_start_training_job(
        uuid="ds-42", options=["models", "lin"], _credentials=GOOD_CREDS,
    )
    assert result["_ui_components"][0]["variant"] == "error"


def test_start_training_job_registers_long_running(rmock: HttpMock) -> None:
    rmock.add("POST", START_JOB_URL, status=200, json={"started": True})
    seen = {}

    class _FakeRuntime:
        def start_long_running_job(self, poll_fn, **_kw):
            seen["poll_fn"] = poll_fn
    runtime = _FakeRuntime()
    mcp_tools.forecaster_start_training_job(
        uuid="ds-42", _credentials=GOOD_CREDS, _runtime=runtime,
    )
    assert callable(seen.get("poll_fn"))


def test_status_poll_maps_completed_to_succeeded(rmock: HttpMock) -> None:
    rmock.add("GET", JOB_STATUS_URL, status=200, json={"status": "Completed"})
    rmock.add("GET", RESULTS_URL, status=200, json={
        "output_log": "ok",
        "file_contents": {"linear-regression": {"rmse": 0.42}},
    })
    client = mcp_tools.make_client(GOOD_CREDS)
    poll = mcp_tools._make_status_poll(client, "ds-42")
    res = poll()
    assert res["status"] == "succeeded"
    assert res["percentage"] == 100
    assert res["result"]["file_contents"]["linear-regression"]["rmse"] == 0.42


def test_status_poll_maps_training_to_in_progress(rmock: HttpMock) -> None:
    rmock.add("GET", JOB_STATUS_URL, status=200, json={"status": "Training: epoch 3/10"})
    client = mcp_tools.make_client(GOOD_CREDS)
    poll = mcp_tools._make_status_poll(client, "ds-42")
    res = poll()
    assert res["status"] == "in_progress"
    assert "Training" in res["message"]


def test_status_poll_unknown_nonempty_status_is_in_progress(rmock: HttpMock) -> None:
    rmock.add("GET", JOB_STATUS_URL, status=200, json={"status": "Initializing"})
    client = mcp_tools.make_client(GOOD_CREDS)
    poll = mcp_tools._make_status_poll(client, "ds-42")
    res = poll()
    assert res["status"] == "in_progress"


def test_status_poll_empty_status_is_failed(rmock: HttpMock) -> None:
    rmock.add("GET", JOB_STATUS_URL, status=200, json={"status": ""})
    client = mcp_tools.make_client(GOOD_CREDS)
    poll = mcp_tools._make_status_poll(client, "ds-42")
    res = poll()
    assert res["status"] == "failed"


def test_get_job_status_renders_card(rmock: HttpMock) -> None:
    rmock.add("GET", JOB_STATUS_URL, status=200, json={"status": "Training: epoch 5/10"})
    result = mcp_tools.forecaster_get_job_status(uuid="ds-42", _credentials=GOOD_CREDS)
    assert result["_data"]["status"] == "in_progress"
    assert result["_ui_components"][0].get("variant") != "error"


def test_get_results_renders_per_model_table(rmock: HttpMock) -> None:
    rmock.add("GET", RESULTS_URL, status=200, json={
        "output_log": "training complete",
        "file_contents": {
            "linear-regression": {"rmse": 0.42, "mae": 0.18},
            "random-forest":     {"rmse": 0.36, "mae": 0.15},
        },
    })
    result = mcp_tools.forecaster_get_results(uuid="ds-42", _credentials=GOOD_CREDS)
    cards = result["_ui_components"]
    contents = cards[0].get("content", [])
    types = [c.get("type") for c in contents if isinstance(c, dict)]
    assert "table" in types
    table = next(c for c in contents if isinstance(c, dict) and c.get("type") == "table")
    assert table["headers"][0] == "Model"
    row_names = sorted(r[0] for r in table["rows"])
    assert row_names == ["linear-regression", "random-forest"]
    assert any(c.get("title") == "Output log" for c in cards if isinstance(c, dict))


def test_get_results_renders_flat_metrics_table(rmock: HttpMock) -> None:
    rmock.add("GET", RESULTS_URL, status=200, json={
        "output_log": "",
        "file_contents": {"rmse": 0.42, "mae": 0.18},
    })
    result = mcp_tools.forecaster_get_results(uuid="ds-42", _credentials=GOOD_CREDS)
    card = result["_ui_components"][0]
    contents = card.get("content", [])
    table = next(c for c in contents if isinstance(c, dict) and c.get("type") == "table")
    assert table["headers"] == ["Metric", "Value"]


def test_get_results_parses_string_encoded_file_contents(rmock: HttpMock) -> None:
    inner = {
        "linear-regression": {"Normalized MAE": 0.118, "R-squared": 0.348},
        "Baseline Average Prediction": {"Normalized MAE": 0.286, "R-squared": -2.25},
    }
    rmock.add("GET", RESULTS_URL, status=200, json={
        "success": True,
        "message": "Metrics retrieved",
        "file_contents": json.dumps(inner),
        "output_log": "training done",
    })
    result = mcp_tools.forecaster_get_results(uuid="ds-42", _credentials=GOOD_CREDS)
    card = result["_ui_components"][0]
    contents = card.get("content", [])
    types = [c.get("type") for c in contents if isinstance(c, dict)]
    assert "table" in types
    table = next(c for c in contents if isinstance(c, dict) and c.get("type") == "table")
    assert table["headers"][0] == "Model"
    assert isinstance(result["_data"]["metrics"], dict)
    assert "linear-regression" in result["_data"]["metrics"]


def test_get_results_auth_failure_renders_alert(rmock: HttpMock) -> None:
    rmock.add("GET", RESULTS_URL, status=401, body=b"{}")
    result = mcp_tools.forecaster_get_results(uuid="ds-42", _credentials=GOOD_CREDS)
    assert result["_ui_components"][0]["variant"] == "error"


def test_delete_dataset_posts_uuid(rmock: HttpMock) -> None:
    rmock.add("POST", DELETE_URL, status=200, json={"deleted": True})
    result = mcp_tools.forecaster_delete_dataset(uuid="ds-42", _credentials=GOOD_CREDS)
    assert result["_ui_components"][0].get("variant") != "error"
    call = rmock.calls[-1]
    assert call["url"] == DELETE_URL
    assert (call.get("data") or {}).get("uuid") == "ds-42"


def test_long_running_tools_set_correct() -> None:
    assert mcp_tools.LONG_RUNNING_TOOLS == {"forecaster_start_training_job"}


def test_tool_registry_has_required_entries() -> None:
    expected = {
        "forecaster_submit_dataset",
        "set_column_roles",
        "forecaster_start_training_job",
        "forecaster_get_job_status",
        "forecaster_get_results",
        "forecaster_delete_dataset",
    }
    assert set(mcp_tools.TOOL_REGISTRY.keys()) == expected


def test_column_roles_match_docs() -> None:
    assert mcp_tools.COLUMN_ROLES == [
        "not-included",
        "time-component",
        "grouping",
        "target",
        "past-covariates",
        "future-covariates",
        "static-covariates",
    ]
