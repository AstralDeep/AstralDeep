"""Derives the agent-authoring LLM prompt spec from astralprims' Pydantic fields so it
can't drift, feeding generate_llm_prompt_section() into agent_generator.py and
agent_validator.py.
"""

import os
import sys
from typing import Dict, Any, Set

_backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from astralprims import (  # noqa: E402
    Text, Card, Table, List_, Alert, ProgressBar, MetricCard,
    CodeBlock, Image, Grids, Tabs, Collapsible, Divider,
    BarChart, LineChart, PieChart, PlotlyChart, Container,
    ColorPicker, FileUpload, FileDownload, Button, Input,
)

COMPONENT_CLASSES = [
    Text, Card, Table, List_, Alert, ProgressBar, MetricCard,
    CodeBlock, Image, Grids, Tabs, Collapsible, Divider,
    BarChart, LineChart, PieChart, PlotlyChart, Container,
    ColorPicker, FileUpload, FileDownload, Button, Input,
]


def _build_primitives_spec() -> Dict[str, Dict[str, Any]]:
    spec = {}
    for cls in COMPONENT_CLASSES:
        field_info = {}
        type_value = None
        for fname, finfo in cls.model_fields.items():
            try:
                default = finfo.get_default(call_default_factory=True)
            except Exception:
                default = None
            field_info[fname] = {
                "type": str(finfo.annotation),
                "default": default,
            }
            if fname == "type" and isinstance(default, str):
                type_value = default

        if type_value:
            spec[type_value] = {
                "class_name": cls.__name__,
                "fields": field_info,
            }
    return spec


PRIMITIVES_SPEC: Dict[str, Dict[str, Any]] = _build_primitives_spec()

VALID_COMPONENT_TYPES: Set[str] = set(PRIMITIVES_SPEC.keys())

_ASTRALPRIMS_IMPORT = """from astralprims import (
    Text, Card, Table, Container, MetricCard, ProgressBar,
    Alert, Grid, BarChart, LineChart, PieChart, PlotlyChart, List_,
    Collapsible, Divider, CodeBlock, Image, Tabs,
    FileDownload, FileUpload, Button, Input, ColorPicker,
    create_ui_response
)"""

REQUIRED_IMPORTS_BLOCK = f"""import os
import sys
from typing import Dict, Any, List, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

{_ASTRALPRIMS_IMPORT}"""

BYO_REQUIRED_IMPORTS_BLOCK = f"""from typing import Dict, Any, List, Optional

{_ASTRALPRIMS_IMPORT}"""

WORKING_EXAMPLE = '''def get_stock_summary(ticker: str, **kwargs) -> Dict[str, Any]:
    """Get a summary for a stock ticker with UI visualization."""
    try:
        # ... fetch data from API ...
        price = 150.25
        change = 2.50
        change_pct = 1.69

        components = [
            Card(
                title=f"Stock Summary: {ticker.upper()}",
                content=[
                    Grid(
                        columns=3,
                        children=[
                            MetricCard(title="Price", value=f"${price:.2f}"),
                            MetricCard(title="Change", value=f"${change:+.2f}", subtitle=f"{change_pct:+.2f}%"),
                            MetricCard(title="Status", value="Active"),
                        ]
                    ),
                    Table(
                        headers=["Metric", "Value"],
                        rows=[
                            ["Open", "$148.00"],
                            ["High", "$151.20"],
                            ["Low", "$147.50"],
                            ["Volume", "12.3M"],
                        ]
                    ),
                    Text(content="Data provided by example API.", variant="caption"),
                ]
            ),
        ]

        return {
            "_ui_components": [c.to_dict() for c in components],
            "_data": {
                "ticker": ticker,
                "price": price,
                "change": change,
                "change_percent": change_pct,
            }
        }
    except Exception as e:
        return create_ui_response([
            Alert(message=f"Failed to fetch data for {ticker}: {str(e)}", variant="error")
        ])'''

def _build_component_reference() -> str:
    lines = []
    for type_val, info in sorted(PRIMITIVES_SPEC.items()):
        cls_name = info["class_name"]
        fields = info["fields"]
        relevant = {k: v for k, v in fields.items()
                    if k not in ("type", "id", "css", "class_name", "tooltip", "attributes")}
        params = []
        for fname, finfo in relevant.items():
            default = finfo["default"]
            if default is None:
                params.append(f"{fname}")
            else:
                params.append(f"{fname}={default!r}")
        lines.append(f"  - {cls_name}({', '.join(params)})  # type=\"{type_val}\"")
    return "\n".join(lines)


COMPONENT_REFERENCE = _build_component_reference()

def generate_llm_prompt_section(self_contained: bool = False) -> str:
    imports_block = BYO_REQUIRED_IMPORTS_BLOCK if self_contained else REQUIRED_IMPORTS_BLOCK
    return f"""## UI COMPONENT SYSTEM — YOU MUST FOLLOW THIS EXACTLY

### Required Imports
Your file MUST start with these imports (after any package imports):
```python
{imports_block}
```

### Available UI Components
These are the ONLY components you can use. Note the exact class names and field names:
{COMPONENT_REFERENCE}

### CRITICAL RULES
1. **Use the primitive classes**, NOT raw dicts. Create component objects and call `.to_dict()`.
2. **Card uses `content`** (a list of child components), NOT `children`.
3. **Grid uses `children`** (a list of child components).
4. **Types are lowercase** in the JSON output (handled by `.to_dict()` automatically).
5. **Every tool MUST return** a dict with BOTH keys:
   - `"_ui_components"`: `[c.to_dict() for c in components]` — a list of serialized component dicts
   - `"_data"`: a dict of raw data for the LLM to reference in its response
6. **Error handling**: Wrap tool body in try/except and return `create_ui_response([Alert(message=str(e), variant="error")])` on failure.
7. **Never return an empty `_ui_components` list** — always include at least one component (even if just a Text or Alert).
8. **MetricCard** is the class name, but its type is `"metric"`. Use `MetricCard(title="...", value="...")`.
9. **List_** (with underscore) is the list component. Use `List_(items=["item1", "item2"])`.

### Complete Working Example
```python
{WORKING_EXAMPLE}
```

### TOOL_REGISTRY Format
Each tool must be registered in TOOL_REGISTRY:
```python
TOOL_REGISTRY = {{
    "tool_name": {{
        "function": tool_function,
        "description": "What this tool does",
        "input_schema": {{
            "type": "object",
            "properties": {{
                "param_name": {{"type": "string", "description": "..."}}
            }},
            "required": ["param_name"]
        }},
        "scope": "tools:read"
    }}
}}
```"""
