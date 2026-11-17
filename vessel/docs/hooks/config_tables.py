# Fills `<!-- config: module.Model -->` markers with a table of the model's fields read from the pydantic models.
# The tables stay in step with the code because they are built from it on every docs build.

from __future__ import annotations

import importlib
import json
import re
import sys
import types
import typing
from pathlib import Path

from pydantic import BaseModel
from pydantic_core import PydanticUndefined

MARKER = re.compile(r"<!-- config: (astralvessel\.[a-z]+)\.([A-Za-z]+) -->")
BOUNDS = (("gt", ">"), ("ge", ">="), ("lt", "<"), ("le", "<="), ("min_length", "min length"),
          ("max_length", "max length"))

# Checks made by validators, which the field metadata cannot show.
CHECKS = {
    "NodeSection": ["`front_address` must be inside `front_subnet`, and `front_subnet` must be a network address "
                    "(`172.31.247.0/24`, not `172.31.247.1/24`).",
                    "`front_subnet` must be a /28 or larger, and `front_address` must be in its lower half; "
                    "other containers on `front` get addresses from the upper half.",
                    "`public_url` must be an `http://` or `https://` origin with no path.",
                    "`environment` names must be upper case, may not be set by the deployment itself and may not "
                    "look like secrets."],
    "OwnerSection": ["Every issuer must be an `http://` or `https://` URL with a host and no commas or spaces."],
    "ToolsSection": ["Every `builtin` entry must be one of `read`, `grep`, `glob`, `list`, `bash`."],
    "DeclaredTool": ["`command` needs at least one element.",
                     "The name may not repeat another declared tool or a built-in tool."],
    "DeploymentFile": ["Agent ids are unique."],
    "SandboxProfile": ["Each mount must be read-only, and the targets must match the agent's `[sandbox] mounts` "
                       "exactly.",
                       "`turn_disk` defaults to a quarter of `scratch` and `user_disk` to half; sizes look like `64m` "
                       "or `2g`, and `0 < turn_disk <= user_disk <= scratch`."],
    "SandboxSection": ["`dockerfile` must be inside the agent folder."],
    "EgressSection": ["`upstream_url`, when set, must be an `http://` or `https://` URL with a host and no spaces, "
                      "or `env:NAME` with a valid variable name."],
}


def _type(annotation: typing.Any) -> str:
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is typing.Literal:
        return " \\| ".join(json.dumps(a) for a in args)
    if origin in (typing.Union, types.UnionType):
        return " \\| ".join(_type(a) for a in args)
    if origin in (list, dict):
        return f"{origin.__name__}[{', '.join(_type(a) for a in args)}]"
    if annotation is type(None):
        return "None"
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return f"[{annotation.__name__}](#{annotation.__name__.lower()})"
    return getattr(annotation, "__name__", str(annotation))


def _value(value: typing.Any) -> str:
    if isinstance(value, BaseModel):
        return "table below" if value.model_dump() else "`{}`"
    if value is None:
        return "none"
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    return f"`{json.dumps(value)}`"


def _default(model: str, name: str, field: typing.Any) -> str:
    if field.is_required():
        return "**required**"
    if field.default is PydanticUndefined:
        return _value(field.default_factory())
    return _value(field.default)


def _constraints(field: typing.Any) -> str:
    found = []
    for meta in field.metadata:
        for attr, sign in BOUNDS:
            if getattr(meta, attr, None) is not None:
                found.append(f"{sign} {getattr(meta, attr)}")
        if getattr(meta, "pattern", None):
            found.append(f"matches `{meta.pattern}`")
    return ", ".join(found)


def table(module: str, name: str) -> str:
    model = getattr(importlib.import_module(module), name)
    rows = ["| Field | Type | Default | Constraints |", "|---|---|---|---|"]
    for field_name, field in model.model_fields.items():
        rows.append(f"| `{field_name}` | {_type(field.annotation)} | {_default(name, field_name, field)} | "
                    f"{_constraints(field)} |")
    checks = CHECKS.get(name)
    if checks:
        rows += ["", "Also checked when the file loads:", ""] + [f"- {line}" for line in checks]
    return "\n".join(rows)


def on_config(config, **kwargs):
    root = str(Path(config.config_file_path).resolve().parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    return config


def on_page_markdown(markdown, page, config, files, **kwargs):
    return MARKER.sub(lambda m: table(m.group(1), m.group(2)), markdown)
