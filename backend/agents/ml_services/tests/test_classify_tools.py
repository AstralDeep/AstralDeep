"""Tests for ml_services/classify_tools.py: credential checks, get_ml_options rendering,
dataset submission (file-handle and inline-data paths), column typing, training-job
start/poll/results, output log, and dataset deletion.
"""

import json
import socket
from unittest.mock import patch

import pytest

from agents.ml_services import classify_tools as mcp_tools
from shared.tests._http_mock import HttpMock


SAFE_HOST = "classify.example.com"
BASE_URL = f"https://{SAFE_HOST}"
GOOD_CREDS = {"CLASSIFY_URL": BASE_URL, "CLASSIFY_API_KEY": "sentinel-api-key"}

ML_OPTS_URL = f"{BASE_URL}/reports/get-ml-opts"
SUBMIT_URL = f"{BASE_URL}/reports/submit"
SET_COLS_URL = f"{BASE_URL}/reports/set-column-changes"
START_JOB_URL = f"{BASE_URL}/reports/start-training-job"
JOB_STATUS_URL = f"{BASE_URL}/reports/get-job-status"
RESULTS_URL = f"{BASE_URL}/result/get-results"
OUTPUT_LOG_URL = f"{BASE_URL}/result/get-output-log"
DELETE_URL = f"{BASE_URL}/reports/delete"


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


@pytest.fixture(autouse=True)
def clear_report_path_cache():
    mcp_tools._REPORT_ATTACHMENTS.clear()
    yield
    mcp_tools._REPORT_ATTACHMENTS.clear()


def test_credentials_check_ok(rmock: HttpMock) -> None:
    rmock.add("GET", ML_OPTS_URL, status=200, json={"parameters": {}})
    result = mcp_tools._credentials_check(_credentials=GOOD_CREDS)
    assert result == {"credential_test": "ok"}
    assert rmock.calls[-1]["url"] == ML_OPTS_URL
    assert rmock.calls[-1].get("params") == {"unsstate": 0}


def test_credentials_check_auth_failed(rmock: HttpMock) -> None:
    rmock.add("GET", ML_OPTS_URL, status=401, body=b"{}")
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
    result = mcp_tools._credentials_check(_credentials={"CLASSIFY_URL": BASE_URL})
    assert result["credential_test"] == "unexpected"


def test_get_ml_options_renders_card(rmock: HttpMock) -> None:
    rmock.add("GET", ML_OPTS_URL, status=200, json={"parameters": {"train_group": {"default": ["randomforest"]}}})
    result = mcp_tools.get_ml_options(_credentials=GOOD_CREDS)
    assert "_ui_components" in result
    assert result["_ui_components"][0].get("variant") != "error"
    assert result["_data"]["parameters"]["train_group"]["default"] == ["randomforest"]


def test_get_ml_options_renders_parameter_table(rmock: HttpMock) -> None:
    rmock.add("GET", ML_OPTS_URL, status=200, json={
        "success": True,
        "message": "",
        "parameters": {
            "parameter_tune": {
                "type": "bool", "default": True,
                "models": ["spectralclustering", "kmeans", "hdbscan"],
                "help": "Perform hyperparameter tuning",
            },
            "num_clusters": {
                "type": "int", "default": 2,
                "models": ["spectralclustering", "kmeans"],
                "help": "Number of clusters to use",
            },
        },
    })
    result = mcp_tools.get_ml_options(_credentials=GOOD_CREDS)
    card = result["_ui_components"][0]
    contents = card.get("content", [])
    types = [c.get("type") for c in contents if isinstance(c, dict)]
    assert "table" in types
    table = next(c for c in contents if isinstance(c, dict) and c.get("type") == "table")
    assert table["headers"] == ["Parameter", "Type", "Default", "Applies to", "Description"]
    row_names = [r[0] for r in table["rows"]]
    assert row_names == ["parameter_tune", "num_clusters"]
    defaults_by_name = {r[0]: r[2] for r in table["rows"]}
    assert defaults_by_name["parameter_tune"] == "True"
    assert defaults_by_name["num_clusters"] == "2"
    applies_by_name = {r[0]: r[3] for r in table["rows"]}
    assert "and " not in applies_by_name["parameter_tune"]


def test_get_ml_options_falls_back_when_no_parameters(rmock: HttpMock) -> None:
    rmock.add("GET", ML_OPTS_URL, status=200, json={"success": False, "message": "no opts"})
    result = mcp_tools.get_ml_options(_credentials=GOOD_CREDS)
    contents = result["_ui_components"][0].get("content", [])
    types = [c.get("type") for c in contents if isinstance(c, dict)]
    assert "table" not in types
    assert "text" in types


def test_get_ml_options_renders_alert_on_auth_failure(rmock: HttpMock) -> None:
    rmock.add("GET", ML_OPTS_URL, status=401, body=b"{}")
    result = mcp_tools.get_ml_options(_credentials=GOOD_CREDS)
    assert result["_ui_components"][0]["variant"] == "error"
    assert "rejected" in result["_ui_components"][0]["message"].lower()


def test_get_ml_options_passes_unsstate(rmock: HttpMock) -> None:
    rmock.add("GET", ML_OPTS_URL, status=200, json={"parameters": {}})
    mcp_tools.get_ml_options(unsstate=1, _credentials=GOOD_CREDS)
    assert rmock.calls[-1].get("params") == {"unsstate": 1}


def test_submit_dataset_returns_uuid(rmock: HttpMock, tmp_path) -> None:
    csv = tmp_path / "data.csv"
    csv.write_text("a,b,target\n1,2,X\n")
    rmock.add("POST", SUBMIT_URL, status=200, json={
        "report_uuid": "rpt-1",
        "column_types": {"data_types": {"a": "integer", "b": "integer", "target": "string"}},
    })
    result = mcp_tools.classify_submit_dataset(
        file_handle=str(csv), _credentials=GOOD_CREDS, user_id="alice",
    )
    assert result["_data"]["report_uuid"] == "rpt-1"
    assert result["_data"]["column_types"] == {
        "a": "integer", "b": "integer", "target": "string",
    }


def test_submit_dataset_retains_identity_not_parser_path(
    rmock: HttpMock,
    tmp_path,
) -> None:
    csv = tmp_path / "data.csv"
    csv.write_text("a,b,target\n1,2,X\n")
    rmock.add("POST", SUBMIT_URL, status=200, json={
        "report_uuid": "rpt-1",
        "column_types": {"data_types": {"a": "integer"}},
    })
    result = mcp_tools.classify_submit_dataset(
        file_handle=str(csv), _credentials=GOOD_CREDS,
        user_id="alice", session_id="chat-123",
    )
    assert result["_data"]["debug_copy_path"] is None
    assert mcp_tools._REPORT_ATTACHMENTS["rpt-1"] == (str(csv), "alice")


def test_submit_dataset_missing_user_id_returns_error(rmock: HttpMock, tmp_path) -> None:
    csv = tmp_path / "data.csv"
    csv.write_text("a\n1\n")
    result = mcp_tools.classify_submit_dataset(file_handle=str(csv), _credentials=GOOD_CREDS)
    assert result["_ui_components"][0]["variant"] == "error"


INLINE_CSV = "Week,Enrollment,Cohort\n1,40,A\n2,42,A\n3,45,B\n"


def test_submit_dataset_inline_data_materializes_and_uploads(rmock: HttpMock, tmp_path) -> None:
    materialized = tmp_path / "inline.csv"

    def fake_materialize(text, user_id, *, extension="csv"):
        assert user_id == "alice"
        assert "Week,Enrollment" in text
        assert extension == "csv"
        materialized.write_text(text)
        return str(materialized)

    rmock.add("POST", SUBMIT_URL, status=200, json={
        "report_uuid": "rpt-inline",
        "column_types": {"data_types": {"Week": "integer", "Enrollment": "integer", "Cohort": "string"}},
    })
    with patch.object(mcp_tools, "materialize_text_attachment",
                      side_effect=fake_materialize) as mock_mat:
        result = mcp_tools.classify_submit_dataset(
            inline_data=INLINE_CSV, _credentials=GOOD_CREDS, user_id="alice",
        )
    mock_mat.assert_called_once()
    assert result["_data"]["report_uuid"] == "rpt-inline"
    assert result["_data"]["column_types"]["Cohort"] == "string"
    call = rmock.calls[-1]
    assert call["url"] == SUBMIT_URL
    assert "file" in (call.get("files") or {})
    assert mcp_tools._REPORT_ATTACHMENTS["rpt-inline"] == (
        str(materialized),
        "alice",
    )


def test_submit_dataset_inline_data_requires_user_id(rmock: HttpMock) -> None:
    with patch.object(mcp_tools, "materialize_text_attachment") as mock_mat:
        result = mcp_tools.classify_submit_dataset(
            inline_data=INLINE_CSV, _credentials=GOOD_CREDS,
        )
    mock_mat.assert_not_called()
    assert result["_ui_components"][0]["variant"] == "error"


def test_submit_dataset_requires_handle_or_inline(rmock: HttpMock) -> None:
    result = mcp_tools.classify_submit_dataset(
        _credentials=GOOD_CREDS, user_id="alice",
    )
    assert result["_ui_components"][0]["variant"] == "error"
    assert "inline_data" in result["_ui_components"][0]["message"]


def test_submit_dataset_file_handle_wins_over_inline(rmock: HttpMock, tmp_path) -> None:
    csv = tmp_path / "real.csv"
    csv.write_text("a,b,target\n1,2,X\n")
    rmock.add("POST", SUBMIT_URL, status=200, json={
        "report_uuid": "rpt-1",
        "column_types": {"data_types": {"a": "integer"}},
    })
    with patch.object(mcp_tools, "materialize_text_attachment") as mock_mat:
        result = mcp_tools.classify_submit_dataset(
            file_handle=str(csv), inline_data=INLINE_CSV,
            _credentials=GOOD_CREDS, user_id="alice",
        )
    mock_mat.assert_not_called()
    assert result["_data"]["report_uuid"] == "rpt-1"


def test_submit_dataset_inline_validation_error_renders_alert(rmock: HttpMock) -> None:
    with patch.object(
        mcp_tools, "materialize_text_attachment",
        side_effect=ValueError("inline_data is not valid CSV: no header row detected."),
    ):
        result = mcp_tools.classify_submit_dataset(
            inline_data="not a csv", _credentials=GOOD_CREDS, user_id="alice",
        )
    assert result["_ui_components"][0]["variant"] == "error"
    assert "not valid csv" in result["_ui_components"][0]["message"].lower()


def test_submit_dataset_schema_offers_inline_data() -> None:
    entry = mcp_tools.TOOL_REGISTRY["classify_submit_dataset"]
    props = entry["input_schema"]["properties"]
    assert "inline_data" in props
    assert "file_handle" in props
    assert entry["input_schema"]["required"] == []
    assert "NEVER invent" in entry["description"]


def test_set_column_types_posts_form_encoded(rmock: HttpMock) -> None:
    rmock.add("POST", SET_COLS_URL, status=200, json={"ok": True})
    column_changes = [
        {"column": "a", "data_type": "integer", "checked": True, "missing": None, "fill_value": None},
        {"column": "target", "data_type": "string", "checked": True, "missing": None,
         "fill_value": None, "class": True},
    ]
    mcp_tools.set_column_types(
        report_uuid="rpt-1", column_changes=column_changes,
        _credentials=GOOD_CREDS,
    )
    call = rmock.calls[-1]
    assert call["url"] == SET_COLS_URL
    sent = call.get("data") or {}
    assert sent.get("report_uuid") == "rpt-1"
    parsed = json.loads(sent["column_changes"])
    assert parsed == column_changes


def test_set_column_types_auto_flags_class_column(rmock: HttpMock) -> None:
    rmock.add("POST", SET_COLS_URL, status=200, json={"ok": True})
    column_changes = [
        {"column": "a", "data_type": "integer", "checked": True},
        {"column": "target", "data_type": "string", "checked": True},
    ]
    mcp_tools.set_column_types(
        report_uuid="rpt-1", column_changes=column_changes,
        class_column="target", _credentials=GOOD_CREDS,
    )
    sent_changes = json.loads(rmock.calls[-1]["data"]["column_changes"])
    target_entry = next(c for c in sent_changes if c["column"] == "target")
    assert target_entry.get("class") is True


def test_set_column_types_rejects_non_list() -> None:
    result = mcp_tools.set_column_types(
        report_uuid="rpt-1", column_changes={"not": "a list"},
        _credentials=GOOD_CREDS,
    )
    assert result["_ui_components"][0]["variant"] == "error"


def test_set_column_types_auto_builds_from_pandas(rmock: HttpMock, tmp_path) -> None:
    rmock.add("POST", SET_COLS_URL, status=200, json={"ok": True})
    csv_path = tmp_path / "tiny.csv"
    csv_path.write_text("a,b,target\n1,,x\n2,3,y\n")
    column_types = {"a": "integer", "b": "integer", "target": "string"}

    result = mcp_tools.set_column_types(
        report_uuid="rpt-1",
        file_handle=str(csv_path),
        class_column="target",
        column_types=column_types,
        _credentials=GOOD_CREDS,
        user_id="dev",
    )
    assert result["_ui_components"][0].get("variant") != "error"
    sent_changes = json.loads(rmock.calls[-1]["data"]["column_changes"])
    by_col = {c["column"]: c for c in sent_changes}

    assert by_col["a"]["missing"] is None
    assert by_col["a"]["data_type"] == "integer"
    assert by_col["a"]["checked"] is True
    assert by_col["a"]["fill_value"] is None
    assert "class" not in by_col["a"]

    assert by_col["b"]["missing"] == "synthetic"
    assert by_col["b"]["fill_value"] is None

    assert by_col["target"].get("class") is True
    assert by_col["target"]["data_type"] == "string"


def test_set_column_types_auto_build_respects_excluded_and_constant(rmock: HttpMock,
                                                                    tmp_path) -> None:
    rmock.add("POST", SET_COLS_URL, status=200, json={"ok": True})
    csv_path = tmp_path / "tiny.csv"
    csv_path.write_text("a,b,target\n1,,x\n2,3,y\n")
    column_types = {"a": "integer", "b": "integer", "target": "string"}

    mcp_tools.set_column_types(
        report_uuid="rpt-1",
        file_handle=str(csv_path),
        class_column="target",
        column_types=column_types,
        missing_strategy="constant",
        fill_value=0,
        excluded_columns=["a"],
        _credentials=GOOD_CREDS,
        user_id="dev",
    )
    sent_changes = json.loads(rmock.calls[-1]["data"]["column_changes"])
    by_col = {c["column"]: c for c in sent_changes}
    assert by_col["a"]["checked"] is False
    assert by_col["b"]["missing"] == "constant"
    assert by_col["b"]["fill_value"] == 0


def test_set_column_types_auto_build_requires_path_or_handle() -> None:
    result = mcp_tools.set_column_types(
        report_uuid="rpt-1", class_column="target",
        _credentials=GOOD_CREDS, user_id="dev",
    )
    assert result["_ui_components"][0]["variant"] == "error"


def test_set_column_types_uses_path_stashed_by_submit_dataset(rmock: HttpMock,
                                                              tmp_path) -> None:
    csv_path = tmp_path / "uploaded.csv"
    csv_path.write_text("a,b,target\n1,,x\n2,3,y\n")
    rmock.add("POST", SUBMIT_URL, status=200, json={
        "report_uuid": "rpt-cached",
        "column_types": {"data_types": {"a": "integer", "b": "integer", "target": "string"}},
    })
    rmock.add("POST", SET_COLS_URL, status=200, json={"ok": True})

    submit_result = mcp_tools.classify_submit_dataset(
        file_handle=str(csv_path),
        _credentials=GOOD_CREDS, user_id="dev", session_id="sess-1",
    )
    assert submit_result["_data"]["report_uuid"] == "rpt-cached"

    result = mcp_tools.set_column_types(
        report_uuid="rpt-cached", class_column="target",
        column_types=submit_result["_data"]["column_types"],
        _credentials=GOOD_CREDS,
    )
    assert result["_ui_components"][0].get("variant") != "error"
    sent_changes = json.loads(rmock.calls[-1]["data"]["column_changes"])
    by_col = {c["column"]: c for c in sent_changes}
    assert by_col["b"]["missing"] == "synthetic"
    assert by_col["target"].get("class") is True


def test_delete_dataset_clears_stashed_attachment(rmock: HttpMock, tmp_path) -> None:
    csv_path = tmp_path / "uploaded.csv"
    csv_path.write_text("a,target\n1,x\n")
    rmock.add("POST", SUBMIT_URL, status=200, json={
        "report_uuid": "rpt-del",
        "column_types": {"data_types": {"a": "integer", "target": "string"}},
    })
    rmock.add("POST", DELETE_URL, status=200, json={"ok": True})

    mcp_tools.classify_submit_dataset(
        file_handle=str(csv_path),
        _credentials=GOOD_CREDS, user_id="dev", session_id="sess-1",
    )
    assert "rpt-del" in mcp_tools._REPORT_ATTACHMENTS

    mcp_tools.classify_delete_dataset(report_uuid="rpt-del", _credentials=GOOD_CREDS)
    assert "rpt-del" not in mcp_tools._REPORT_ATTACHMENTS


class _FakeRuntime:
    def __init__(self):
        self.scheduled = []

    def start_long_running_job(self, poll_fn):
        self.scheduled.append(poll_fn)


def test_start_training_job_returns_report_uuid_and_starts_poller(rmock: HttpMock) -> None:
    rmock.add("POST", START_JOB_URL, status=200, json={"queued": True})
    runtime = _FakeRuntime()
    result = mcp_tools.classify_start_training_job(
        report_uuid="rpt-1", class_column="target",
        options=[{"name": "parameter_tune", "value": False}],
        _credentials=GOOD_CREDS, _runtime=runtime,
    )
    assert result["_data"]["report_uuid"] == "rpt-1"
    assert result["_data"]["status"] == "started"
    assert len(runtime.scheduled) == 1
    sent_options = json.loads(rmock.calls[-1]["data"]["options"])
    names_to_values = {entry["name"]: entry["value"] for entry in sent_options}
    assert names_to_values["report_uuid"] == "rpt-1"
    assert names_to_values["class_column"] == "target"
    assert names_to_values["supervised"] == "True"
    assert names_to_values["autodetermineclusters"] == "False"
    assert names_to_values["parameter_tune"] is False


def test_start_training_job_without_runtime_still_returns_ack(rmock: HttpMock) -> None:
    rmock.add("GET", ML_OPTS_URL, status=200, json={"parameters": {}})
    rmock.add("POST", START_JOB_URL, status=200, json={})
    result = mcp_tools.classify_start_training_job(
        report_uuid="rpt-1", class_column="target",
        _credentials=GOOD_CREDS,
    )
    assert result["_data"]["status"] == "started"


def test_start_training_job_auto_filters_train_group(rmock: HttpMock) -> None:
    rmock.add("GET", ML_OPTS_URL, status=200, json={
        "parameters": {
            "train_group": {
                "type": "string",
                "default": ["randomforest", "gradientboosting", "xgboost"],
            },
            "parameter_tune": {"type": "bool", "default": True},
            "num_estimators": {"type": "int", "default": 100},
        },
    })
    rmock.add("POST", START_JOB_URL, status=200, json={"queued": True})
    result = mcp_tools.classify_start_training_job(
        report_uuid="rpt-9", class_column="target",
        _credentials=GOOD_CREDS,
    )
    assert result["_data"]["status"] == "started"
    sent_options = json.loads(rmock.calls[-1]["data"]["options"])

    train_group_entries = [e for e in sent_options if e["name"] == "train_group"]
    assert [e["value"] for e in train_group_entries] == ["randomforest", "gradientboosting"]

    names_to_values = {e["name"]: e["value"] for e in sent_options if e["name"] != "train_group"}
    assert names_to_values["parameter_tune"] is False
    assert names_to_values["num_estimators"] == 100
    assert names_to_values["report_uuid"] == "rpt-9"
    assert names_to_values["class_column"] == "target"
    assert names_to_values["supervised"] == "True"
    assert names_to_values["autodetermineclusters"] == "False"


def test_start_training_job_falls_back_when_train_group_intersection_empty(
        rmock: HttpMock) -> None:
    rmock.add("GET", ML_OPTS_URL, status=200, json={
        "parameters": {
            "parameter_tune": {"default": True},
        },
    })
    rmock.add("POST", START_JOB_URL, status=200, json={})
    mcp_tools.classify_start_training_job(
        report_uuid="rpt-9", class_column="target",
        _credentials=GOOD_CREDS,
    )
    sent_options = json.loads(rmock.calls[-1]["data"]["options"])
    train_group_values = [e["value"] for e in sent_options if e["name"] == "train_group"]
    assert train_group_values == ["randomforest", "gradientboosting"]


def test_start_training_job_falls_back_when_models_dont_match_upstream(
        rmock: HttpMock) -> None:
    rmock.add("GET", ML_OPTS_URL, status=200, json={
        "parameters": {
            "train_group": {"default": ["xgboost", "lightgbm"]},
        },
    })
    rmock.add("POST", START_JOB_URL, status=200, json={})
    mcp_tools.classify_start_training_job(
        report_uuid="rpt-9", class_column="target",
        models_to_train=["randomforest"],
        _credentials=GOOD_CREDS,
    )
    sent_options = json.loads(rmock.calls[-1]["data"]["options"])
    train_group_values = [e["value"] for e in sent_options if e["name"] == "train_group"]
    assert train_group_values == ["randomforest"]


def test_propose_training_config_returns_param_picker(rmock: HttpMock) -> None:
    rmock.add("GET", ML_OPTS_URL, status=200, json={
        "parameters": {
            "train_group": {"type": "string",
                            "default": ["randomforest", "gradientboosting", "xgboost"]},
            "parameter_tune": {"type": "bool", "default": True, "help": "Tune?"},
            "n_iter": {"type": "int", "default": 100, "help": "Iterations"},
            "loss": {"type": "string", "default": "squared_error"},
            "parameter_goal": {
                "f1_macro": "f1 macro",
                "precision_macro": "precision macro",
                "recall_macro": "recall macro",
                "accuracy": "accuracy",
            },
        },
    })
    result = mcp_tools.propose_training_config(
        report_uuid="rpt-pp", class_column="target",
        _credentials=GOOD_CREDS,
    )
    component = result["_ui_components"][0]
    assert component["type"] == "param_picker"
    assert component["submit_label"] == "Start training"
    assert "rpt-pp" in component["submit_message_template"]
    assert "{train_group}" in component["submit_message_template"]
    assert "{__values_json__}" in component["submit_message_template"]
    assert "call classify_start_training_job" in component["submit_message_template"]

    by_name = {f["name"]: f for f in component["fields"]}
    assert by_name["__supervised__"]["kind"] == "boolean"
    assert by_name["__supervised__"]["default"] is True
    assert by_name["__autodetermineclusters__"]["kind"] == "boolean"
    assert by_name["__autodetermineclusters__"]["default"] is False

    assert by_name["train_group"]["kind"] == "checklist"
    assert by_name["train_group"]["options"] == ["randomforest", "gradientboosting", "xgboost"]
    assert by_name["train_group"]["default"] == ["randomforest", "gradientboosting"]

    assert by_name["parameter_tune"]["kind"] == "boolean"
    assert by_name["parameter_tune"]["default"] is True

    assert by_name["n_iter"]["kind"] == "number"
    assert by_name["n_iter"]["default"] == 100

    assert by_name["loss"]["kind"] == "text"
    assert by_name["loss"]["default"] == "squared_error"

    assert by_name["parameter_goal"]["kind"] == "select"
    assert by_name["parameter_goal"]["options"] == \
        ["f1_macro", "precision_macro", "recall_macro", "accuracy"]
    assert by_name["parameter_goal"]["default"] == "f1_macro"


def test_propose_training_config_uses_unsstate_1_when_supervised(rmock: HttpMock) -> None:
    rmock.add("GET", ML_OPTS_URL, status=200, json={
        "parameters": {"train_group": {"default": ["randomforest"]}},
    })
    mcp_tools.propose_training_config(
        report_uuid="rpt", class_column="target",
        _credentials=GOOD_CREDS,
    )
    ml_calls = [c for c in rmock.calls if c.get("url") == ML_OPTS_URL]
    assert ml_calls[0].get("params") == {"unsstate": 1}


def test_propose_training_config_handles_empty_parameters(rmock: HttpMock) -> None:
    rmock.add("GET", ML_OPTS_URL, status=200, json={"parameters": {}})
    result = mcp_tools.propose_training_config(
        report_uuid="rpt", class_column="target",
        _credentials=GOOD_CREDS,
    )
    assert result["_ui_components"][0]["variant"] == "error"


def test_propose_training_config_registered_as_read_scope() -> None:
    entry = mcp_tools.TOOL_REGISTRY["propose_training_config"]
    assert entry["scope"] == "tools:read"
    assert entry["function"] is mcp_tools.propose_training_config


def test_start_training_job_supervised_uses_unsstate_1(rmock: HttpMock) -> None:
    rmock.add("GET", ML_OPTS_URL, status=200, json={
        "parameters": {"train_group": {"default": ["randomforest"]}},
    })
    rmock.add("POST", START_JOB_URL, status=200, json={})
    mcp_tools.classify_start_training_job(
        report_uuid="rpt-9", class_column="target",
        _credentials=GOOD_CREDS,
    )
    ml_calls = [c for c in rmock.calls if c.get("url") == ML_OPTS_URL]
    assert ml_calls
    assert ml_calls[0].get("params") == {"unsstate": 1}


def test_start_training_job_unsupervised_uses_unsstate_0(rmock: HttpMock) -> None:
    rmock.add("GET", ML_OPTS_URL, status=200, json={"parameters": {}})
    rmock.add("POST", START_JOB_URL, status=200, json={})
    mcp_tools.classify_start_training_job(
        report_uuid="rpt-9", class_column="target",
        supervised=False,
        _credentials=GOOD_CREDS,
    )
    ml_calls = [c for c in rmock.calls if c.get("url") == ML_OPTS_URL]
    assert ml_calls
    assert ml_calls[0].get("params") == {"unsstate": 0}


def test_start_training_job_unsstate_override_respected(rmock: HttpMock) -> None:
    rmock.add("GET", ML_OPTS_URL, status=200, json={"parameters": {}})
    rmock.add("POST", START_JOB_URL, status=200, json={})
    mcp_tools.classify_start_training_job(
        report_uuid="rpt-9", class_column="target",
        supervised=True, unsstate=0,
        _credentials=GOOD_CREDS,
    )
    ml_calls = [c for c in rmock.calls if c.get("url") == ML_OPTS_URL]
    assert ml_calls
    assert ml_calls[0].get("params") == {"unsstate": 0}


def test_start_training_job_parameter_overrides_win(rmock: HttpMock) -> None:
    rmock.add("GET", ML_OPTS_URL, status=200, json={
        "parameters": {
            "train_group": {"default": ["randomforest"]},
            "parameter_tune": {"default": True},
            "num_estimators": {"default": 100},
        },
    })
    rmock.add("POST", START_JOB_URL, status=200, json={})
    mcp_tools.classify_start_training_job(
        report_uuid="rpt-9", class_column="target",
        parameter_overrides={"num_estimators": 500, "parameter_tune": True},
        _credentials=GOOD_CREDS,
    )
    sent_options = json.loads(rmock.calls[-1]["data"]["options"])
    names_to_values = {e["name"]: e["value"] for e in sent_options if e["name"] != "train_group"}
    assert names_to_values["num_estimators"] == 500
    assert names_to_values["parameter_tune"] is True


_PARAMETER_GOAL_META = {
    "f1_macro": "f1 macro",
    "precision_macro": "precision macro",
    "recall_macro": "recall macro",
    "accuracy": "accuracy",
}


def test_start_training_job_parameter_goal_uses_override(rmock: HttpMock) -> None:
    rmock.add("GET", ML_OPTS_URL, status=200, json={
        "parameters": {
            "train_group": {"default": ["randomforest"]},
            "parameter_goal": _PARAMETER_GOAL_META,
        },
    })
    rmock.add("POST", START_JOB_URL, status=200, json={})
    mcp_tools.classify_start_training_job(
        report_uuid="rpt-9", class_column="target",
        parameter_overrides={"parameter_goal": "accuracy"},
        _credentials=GOOD_CREDS,
    )
    sent_options = json.loads(rmock.calls[-1]["data"]["options"])
    by_name = {e["name"]: e["value"] for e in sent_options if e["name"] != "train_group"}
    assert by_name["parameter_goal"] == "accuracy"


def test_start_training_job_parameter_goal_defaults_to_f1_macro(rmock: HttpMock) -> None:
    rmock.add("GET", ML_OPTS_URL, status=200, json={
        "parameters": {
            "train_group": {"default": ["randomforest"]},
            "parameter_goal": _PARAMETER_GOAL_META,
        },
    })
    rmock.add("POST", START_JOB_URL, status=200, json={})
    mcp_tools.classify_start_training_job(
        report_uuid="rpt-9", class_column="target",
        _credentials=GOOD_CREDS,
    )
    sent_options = json.loads(rmock.calls[-1]["data"]["options"])
    by_name = {e["name"]: e["value"] for e in sent_options if e["name"] != "train_group"}
    assert by_name["parameter_goal"] == "f1_macro"


def test_start_training_job_parameter_goal_invalid_override_falls_back(rmock: HttpMock) -> None:
    rmock.add("GET", ML_OPTS_URL, status=200, json={
        "parameters": {
            "train_group": {"default": ["randomforest"]},
            "parameter_goal": _PARAMETER_GOAL_META,
        },
    })
    rmock.add("POST", START_JOB_URL, status=200, json={})
    mcp_tools.classify_start_training_job(
        report_uuid="rpt-9", class_column="target",
        parameter_overrides={"parameter_goal": "made_up_goal"},
        _credentials=GOOD_CREDS,
    )
    sent_options = json.loads(rmock.calls[-1]["data"]["options"])
    by_name = {e["name"]: e["value"] for e in sent_options if e["name"] != "train_group"}
    assert by_name["parameter_goal"] == "f1_macro"


def test_status_poll_processed_fetches_results(rmock: HttpMock) -> None:
    rmock.add("GET", JOB_STATUS_URL, status=200, json={"status": "Processed"})
    rmock.add("GET", RESULTS_URL, status=200, json={"accuracy": 0.92})
    client = mcp_tools.make_client(GOOD_CREDS)
    poll = mcp_tools._make_status_poll(client, "rpt-1")
    out = poll()
    assert out["status"] == "succeeded"
    assert out["percentage"] == 100
    assert out["result"] == {"accuracy": 0.92}


def test_status_poll_partial_progress_extracts_percentage(rmock: HttpMock) -> None:
    rmock.add("GET", JOB_STATUS_URL, status=200, json={"status": "3/10 Processed"})
    client = mcp_tools.make_client(GOOD_CREDS)
    out = mcp_tools._make_status_poll(client, "rpt-1")()
    assert out["status"] == "in_progress"
    assert out["percentage"] == 30
    assert out["message"] == "3/10 Processed"


def test_status_poll_processing_in_progress(rmock: HttpMock) -> None:
    rmock.add("GET", JOB_STATUS_URL, status=200, json={"status": "Processing"})
    client = mcp_tools.make_client(GOOD_CREDS)
    out = mcp_tools._make_status_poll(client, "rpt-1")()
    assert out["status"] == "in_progress"
    assert out["percentage"] is None


def test_status_poll_unknown_status_treated_as_failure(rmock: HttpMock) -> None:
    rmock.add("GET", JOB_STATUS_URL, status=200, json={"status": "ERROR: bad CSV"})
    client = mcp_tools.make_client(GOOD_CREDS)
    out = mcp_tools._make_status_poll(client, "rpt-1")()
    assert out["status"] == "failed"
    assert "bad CSV" in out["message"]


def test_get_results_renders_flat_metrics_table(rmock: HttpMock) -> None:
    rmock.add("GET", RESULTS_URL, status=200, json={"accuracy": 0.85, "f1": 0.81})
    result = mcp_tools.classify_get_results(report_uuid="rpt-1", _credentials=GOOD_CREDS)
    assert result["_ui_components"][0].get("variant") != "error"
    assert result["_data"]["results"] == {"accuracy": 0.85, "f1": 0.81}
    card = result["_ui_components"][0]
    contents = card.get("content", [])
    table = next(c for c in contents if isinstance(c, dict) and c.get("type") == "table")
    assert table["headers"] == ["Metric", "Value"]
    rows_by_metric = {r[0]: r[1] for r in table["rows"]}
    assert rows_by_metric["accuracy"] == "0.8500"
    assert rows_by_metric["f1"] == "0.8100"


def test_get_results_renders_per_model_table(rmock: HttpMock) -> None:
    rmock.add("GET", RESULTS_URL, status=200, json={
        "randomforest":    {"accuracy": 0.92, "f1": 0.91},
        "gradientboosting": {"accuracy": 0.88, "f1": 0.87, "roc_auc": 0.95},
    })
    result = mcp_tools.classify_get_results(report_uuid="rpt-1", _credentials=GOOD_CREDS)
    card = result["_ui_components"][0]
    contents = card.get("content", [])
    table = next(c for c in contents if isinstance(c, dict) and c.get("type") == "table")
    assert table["headers"] == ["Model", "accuracy", "f1", "roc_auc"]
    rows_by_model = {r[0]: r[1:] for r in table["rows"]}
    assert rows_by_model["randomforest"][0] == "0.9200"
    assert rows_by_model["randomforest"][2] in ("None", "—")


def test_get_results_falls_back_on_non_dict(rmock: HttpMock) -> None:
    rmock.add("GET", RESULTS_URL, status=200, body=b"raw text output, not JSON")
    result = mcp_tools.classify_get_results(report_uuid="rpt-1", _credentials=GOOD_CREDS)
    card = result["_ui_components"][0]
    contents = card.get("content", [])
    types = [c.get("type") for c in contents if isinstance(c, dict)]
    assert "table" not in types
    assert "text" in types


def test_get_output_log_truncates_long_text(rmock: HttpMock) -> None:
    big = b"x" * 10000
    rmock.add("GET", OUTPUT_LOG_URL, status=200, body=big)
    result = mcp_tools.get_output_log(report_uuid="rpt-1", _credentials=GOOD_CREDS)
    rendered = result["_ui_components"][0]
    text_blocks = rendered.get("content", []) if isinstance(rendered, dict) else []
    if text_blocks and isinstance(text_blocks, list):
        rendered_text = next((b.get("content", "") for b in text_blocks if isinstance(b, dict)), "")
        assert len(rendered_text) <= 4100


def test_delete_dataset_posts_report_uuid(rmock: HttpMock) -> None:
    rmock.add("POST", DELETE_URL, status=200, json={"ok": True})
    mcp_tools.classify_delete_dataset(report_uuid="rpt-1", _credentials=GOOD_CREDS)
    call = rmock.calls[-1]
    assert call["url"] == DELETE_URL
    assert call.get("data") == {"report_uuid": "rpt-1"}


def test_long_running_tools_set_correct() -> None:
    assert mcp_tools.LONG_RUNNING_TOOLS == {"classify_start_training_job"}


def test_tool_registry_has_required_entries() -> None:
    expected = {
        "classify_submit_dataset",
        "set_column_types",
        "get_ml_options",
        "propose_training_config",
        "classify_start_training_job",
        "classify_get_job_status",
        "classify_get_results",
        "get_output_log",
        "classify_delete_dataset",
    }
    assert set(mcp_tools.TOOL_REGISTRY.keys()) == expected


def test_get_job_status_renders_card(rmock: HttpMock) -> None:
    rmock.add("GET", JOB_STATUS_URL, status=200, json={"status": "Processing"})
    result = mcp_tools.classify_get_job_status(report_uuid="rpt-1", _credentials=GOOD_CREDS)
    assert result["_data"]["status"] == "in_progress"
    assert result["_ui_components"][0].get("variant") != "error"


def test_no_api_key_in_response_data(rmock: HttpMock) -> None:
    rmock.add("GET", ML_OPTS_URL, status=200, json={"parameters": {}})
    result = mcp_tools.get_ml_options(_credentials=GOOD_CREDS)
    serialized = str(result)
    assert "sentinel-api-key" not in serialized
