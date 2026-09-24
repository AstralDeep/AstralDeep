"""Static guard confirming the six retired agents leave no directories or backend
imports, and that orchestrator.py's retirement set covers every retired identity.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

REMOVED_PACKAGES = (
    "email_tracker", "grant_budgets", "grants", "linkedin", "nefarious", "nocodb",
    "classify", "forecaster", "llm_factory",
)
SKIP_DIRS = {"__pycache__", "tmp", "data", "node_modules", ".venv"}


def test_retired_agent_directories_are_gone():
    agents_dir = BACKEND_DIR / "agents"
    present = {p.name for p in agents_dir.iterdir() if p.is_dir()}
    leftovers = present & set(REMOVED_PACKAGES)
    assert not leftovers, f"retired agent directories still exist: {sorted(leftovers)}"


def test_no_backend_module_imports_removed_packages():
    removed_modules = {f"agents.{name}" for name in REMOVED_PACKAGES}
    offenders = []
    for py in BACKEND_DIR.rglob("*.py"):
        if any(part in SKIP_DIRS for part in py.parts):
            continue
        if py == Path(__file__).resolve():
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if any(name == m or name.startswith(m + ".") for m in removed_modules):
                    offenders.append(f"{py.relative_to(BACKEND_DIR)}: {name}")
    assert not offenders, "dangling imports of removed agents:\n" + "\n".join(offenders)


def test_retirement_set_covers_all_retired_identities():
    from orchestrator.orchestrator import RETIRED_AGENT_IDS, remap_merged_source

    for hyphen_id in ("email-tracker-1", "grant-budgets-1", "grants-1",
                      "linkedin-1", "nefarious-1", "nocodb-1"):
        assert hyphen_id in RETIRED_AGENT_IDS
    for merged in ("classify-1", "forecaster-1", "llm-factory-1"):
        assert merged not in RETIRED_AGENT_IDS
        new_agent, _ = remap_merged_source(merged, "any_tool")
        assert new_agent == "ml-services-1"


def test_expected_agent_catalog_directories():
    agents_dir = BACKEND_DIR / "agents"
    present = {p.name for p in agents_dir.iterdir()
               if p.is_dir() and p.name not in SKIP_DIRS and not p.name.startswith((".", "__"))}
    expected = {"connectors", "dice_roller", "general",
                "journal_review", "medical", "weather",
                "ml_services", "web_research", "summarizer"}
    missing = expected - present
    assert not missing, f"expected agents missing: {sorted(missing)}"
