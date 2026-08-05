import importlib.util
import os
import sys
import csv

# Add backend to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from agents.general.mcp_tools import modify_data


def _confined_source_path(name: str, user_id: str = "legacy") -> str:
    """Build a source path modify_data will accept.

    ``file_path`` is confined to the caller's own directories, so a fixture
    file has to live under ``backend/tmp/<user_id>/`` — an arbitrary temp
    path is refused (H3-1).
    """
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    user_dir = os.path.join(backend_dir, "tmp", user_id)
    os.makedirs(user_dir, exist_ok=True)
    return os.path.join(user_dir, name)


def test_basic_add_column():
    """Test backward compatibility: add column with static value."""
    csv_data = "name,age\nAlice,25\nBob,30"
    modifications = [
        {"action": "add_column", "name": "status", "value": "active"}
    ]
    result = modify_data(csv_data=csv_data, modifications=modifications)
    assert "_ui_components" in result
    assert "_data" in result
    data = result["_data"]
    assert data["rows_count"] == 2
    # Check file exists
    assert os.path.exists(data["file_path"])
    # Verify content
    with open(data["file_path"], 'r') as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        assert "status" in rows[0]
        assert rows[0]["status"] == "active"
        assert rows[1]["status"] == "active"
    # Cleanup
    os.remove(data["file_path"])


def test_expression_calculation():
    """Test row-based calculation using expression."""
    csv_data = "quantity,price\n2,10\n5,20"
    modifications = [
        {
            "action": "calculate_column",
            "name": "total",
            "expression": "int(row['quantity']) * int(row['price'])"
        }
    ]
    result = modify_data(csv_data=csv_data, modifications=modifications)
    data = result["_data"]
    with open(data["file_path"], 'r') as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        assert rows[0]["total"] == "20"  # 2*10
        assert rows[1]["total"] == "100"  # 5*20
    os.remove(data["file_path"])


def test_conditional_expression():
    """Test conditional expression with if-else."""
    csv_data = "score\n85\n45\n92"
    modifications = [
        {
            "action": "add_column",
            "name": "grade",
            "expression": "'Pass' if int(row['score']) >= 60 else 'Fail'"
        }
    ]
    result = modify_data(csv_data=csv_data, modifications=modifications)
    data = result["_data"]
    with open(data["file_path"], 'r') as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        assert rows[0]["grade"] == "Pass"
        assert rows[1]["grade"] == "Fail"
        assert rows[2]["grade"] == "Pass"
    os.remove(data["file_path"])


def test_expression_with_default():
    """Test expression with default fallback."""
    csv_data = "x,y\n5,10\n0,0"
    modifications = [
        {
            "action": "calculate_column",
            "name": "ratio",
            "expression": "float(row['x']) / float(row['y'])",
            "default": "N/A"
        }
    ]
    result = modify_data(csv_data=csv_data, modifications=modifications)
    data = result["_data"]
    with open(data["file_path"], 'r') as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        # First row: 5/10 = 0.5
        assert rows[0]["ratio"] == "0.5"
        # Second row division by zero -> default
        assert rows[1]["ratio"] == "N/A"
    os.remove(data["file_path"])


def test_dtype_conversion():
    """Test data type conversion."""
    csv_data = "value\n3.14\n2.71"
    modifications = [
        {
            "action": "add_column",
            "name": "int_value",
            "value": "3.14",
            "dtype": "integer"
        }
    ]
    result = modify_data(csv_data=csv_data, modifications=modifications)
    data = result["_data"]
    with open(data["file_path"], 'r') as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        # Should be converted to int 3
        assert rows[0]["int_value"] == "3"
        assert rows[1]["int_value"] == "3"  # same value for all rows
    os.remove(data["file_path"])


def test_excel_support_if_available():
    """Test Excel file support (requires pandas)."""
    # Skip if pandas not installed
    try:
        import pandas as pd
    except ImportError:
        print("Pandas not installed, skipping Excel test")
        return
    
    excel_path = _confined_source_path("modify_data_src.xlsx")
    df = pd.DataFrame({"A": [1, 2, 3], "B": [4, 5, 6]})
    df.to_excel(excel_path, index=False)

    modifications = [
        {"action": "add_column", "name": "C", "expression": "row['A'] + row['B']"}
    ]
    result = modify_data(file_path=excel_path, modifications=modifications, output_format="excel")
    data = result["_data"]
    assert data["output_format"] == "excel"
    # Load result and verify
    df_out = pd.read_excel(data["file_path"])
    assert "C" in df_out.columns
    assert list(df_out["C"]) == [5, 7, 9]
    
    # Cleanup
    os.remove(excel_path)
    os.remove(data["file_path"])


def test_output_format_conversion():
    """Test conversion between CSV and Excel formats."""
    csv_data = "id,name\n1,Alice\n2,Bob"
    modifications = [{"action": "add_column", "name": "extra", "value": "x"}]
    # Request Excel output (if pandas available)
    if importlib.util.find_spec("pandas") is not None:
        result = modify_data(csv_data=csv_data, modifications=modifications, output_format="excel")
        data = result["_data"]
        assert data["output_format"] == "excel"
        assert data["file_path"].endswith(".xlsx")
        os.remove(data["file_path"])
    else:
        # pandas not installed, should fallback to CSV
        result = modify_data(csv_data=csv_data, modifications=modifications, output_format="excel")
        data = result["_data"]
        assert data["output_format"] == "csv"  # fallback
        assert data["file_path"].endswith(".csv")
        os.remove(data["file_path"])


def test_invalid_expression():
    """Test that invalid expression returns error."""
    csv_data = "x\n1"
    modifications = [
        {"action": "add_column", "name": "bad", "expression": "row['missing'] +"}  # syntax error
    ]
    result = modify_data(csv_data=csv_data, modifications=modifications)
    # Should have error alert in UI components
    assert "_ui_components" in result
    # The function returns a UI response with error, not raising exception
    # We'll just ensure it didn't crash
    assert "_data" in result
    if result["_data"] is not None:
        assert "file_path" not in result["_data"]


def test_backward_compatibility_with_file():
    """Original test from file."""
    src_file = _confined_source_path("src_test.csv")
    with open(src_file, 'w') as f:
        f.write("Code,Title\nA00,Cholera\nA01,Typhoid\nA02,Vibrio")

    modifications = [
        {"action": "add_column", "name": "processed", "value": "true"}
    ]

    result = modify_data(file_path=src_file, modifications=modifications, filename="file_modified.csv")

    file_path = result["_data"]["file_path"]
    assert os.path.exists(file_path)
    with open(file_path, 'r') as f:
        content = f.read()
        assert "true" in content and "processed" in content and "Vibrio" in content
    
    # Cleanup
    if os.path.exists(src_file):
        os.remove(src_file)
    os.remove(file_path)


if __name__ == "__main__":
    # Run all tests
    test_basic_add_column()
    print("✓ test_basic_add_column passed")
    test_expression_calculation()
    print("✓ test_expression_calculation passed")
    test_conditional_expression()
    print("✓ test_conditional_expression passed")
    test_expression_with_default()
    print("✓ test_expression_with_default passed")
    test_dtype_conversion()
    print("✓ test_dtype_conversion passed")
    test_excel_support_if_available()
    print("✓ test_excel_support_if_available passed")
    test_output_format_conversion()
    print("✓ test_output_format_conversion passed")
    test_invalid_expression()
    print("✓ test_invalid_expression passed")
    test_backward_compatibility_with_file()
    print("✓ test_backward_compatibility_with_file passed")
    print("\nAll tests passed!")
