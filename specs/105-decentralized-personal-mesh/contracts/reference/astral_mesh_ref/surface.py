"""Surface documents: validation against a vocabulary and a turn's authority, patches, the deterministic fold of puts and patches, action events and value bindings.
Event bodies of type ui.surface_put, ui.patch and ui.action are interpreted here, and the vocabulary itself comes from the primitives schema.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from .errors import MeshError

ID = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
PROVENANCE = ("model", "tool", "host")
MAX_NODES = 512
MAX_DEPTH = 32
URL_PROPS = ("href", "src")
COMPLETE_PROPS = ("name", "spoken", "compact")
DOC_FIELDS = {"capabilities", "components", "root", "surface", "v"}
COMPONENT_FIELDS = {"children", "id", "props", "prov", "type"}
CAP_FIELDS = {"args", "consent", "free", "max_uses", "tool"}


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, bool)) or type(value) is int


def _walk_json(value: Any) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _walk_json(item)
    elif isinstance(value, list):
        for item in value:
            _walk_json(item)
    elif isinstance(value, float):
        raise MeshError("surface.malformed", "floats are refused")


def validate_document(
    doc: Any,
    *,
    vocabulary: set[str],
    actionable: set[str],
    turn: dict[str, Any],
    producer: str = "model",
) -> None:
    if not isinstance(doc, dict) or set(doc) != DOC_FIELDS or doc["v"] != 1:
        raise MeshError("surface.malformed", "document fields")
    components = doc["components"]
    if (
        not isinstance(components, dict)
        or not components
        or len(components) > MAX_NODES
    ):
        raise MeshError("surface.malformed", "components")
    if doc["root"] not in components:
        raise MeshError("surface.malformed", "root component is missing")
    capabilities = doc["capabilities"]
    if not isinstance(capabilities, dict):
        raise MeshError("surface.malformed", "capabilities")
    for cid, capability in capabilities.items():
        if (
            not ID.match(cid)
            or not isinstance(capability, dict)
            or set(capability) != CAP_FIELDS
        ):
            raise MeshError("surface.malformed", "capability")
        if (
            capability["consent"] not in ("required", "policy")
            or type(capability["max_uses"]) is not int
            or capability["max_uses"] < 1
        ):
            raise MeshError("surface.malformed", "capability consent or max_uses")
        if (
            not isinstance(capability["args"], dict)
            or not isinstance(capability["free"], list)
            or capability["free"] != sorted(set(capability["free"]))
        ):
            raise MeshError("surface.malformed", "capability args")
        _walk_json(capability["args"])
        if capability["tool"] not in turn["tools"]:
            raise MeshError(
                "surface.capability_exceeds_turn", f"tool {capability['tool']}"
            )
        if capability["max_uses"] > turn["max_uses"]:
            raise MeshError("surface.capability_exceeds_turn", "max_uses")
        if (
            capability["consent"] == "policy"
            and turn["tool_consent"].get(capability["tool"]) == "required"
        ):
            raise MeshError(
                "surface.capability_exceeds_turn", "consent cannot be downgraded"
            )
    parents: dict[str, str] = {}
    for cid, comp in components.items():
        if (
            not ID.match(cid)
            or not isinstance(comp, dict)
            or set(comp) != COMPONENT_FIELDS
            or comp["id"] != cid
        ):
            raise MeshError("surface.malformed", f"component {cid}")
        if comp["type"] not in vocabulary:
            raise MeshError("surface.unknown_type", str(comp["type"]))
        if comp["prov"] not in PROVENANCE or (
            producer == "model" and comp["prov"] == "host"
        ):
            raise MeshError("surface.false_provenance", cid)
        if not isinstance(comp["props"], dict) or not isinstance(
            comp["children"], list
        ):
            raise MeshError("surface.malformed", f"component {cid}")
        _walk_json(comp["props"])
        for child in comp["children"]:
            if child not in components or child in parents or child == doc["root"]:
                raise MeshError("surface.malformed", f"child {child} of {cid}")
            parents[child] = cid
        props = comp["props"]
        for name in URL_PROPS:
            if name in props and not (
                isinstance(props[name], dict) and set(props[name]) == {"$handle"}
            ):
                raise MeshError("surface.raw_url", f"{cid}.{name}")
        for value in props.values():
            if isinstance(value, dict) and "$bind" in value:
                bind = value["$bind"]
                if (
                    set(value) != {"$bind"}
                    or not isinstance(bind, dict)
                    or set(bind) != {"event", "path"}
                ):
                    raise MeshError("surface.malformed", f"binding in {cid}")
        action = props.get("action")
        if action is not None:
            if (
                not isinstance(action, dict)
                or set(action) != {"capability"}
                or action["capability"] not in capabilities
            ):
                raise MeshError("surface.unknown_capability", cid)
        if comp["type"] in actionable or action is not None:
            if action is None or any(
                not isinstance(props.get(p), str) or not props[p].strip()
                for p in COMPLETE_PROPS
            ):
                raise MeshError(
                    "surface.incomplete",
                    f"{cid} needs an action, a name, a spoken form and a compact form",
                )

    def depth(node: str, seen: frozenset[str]) -> int:
        if node in seen:
            raise MeshError("surface.malformed", "cycle")
        kids = components[node]["children"]
        return 1 + max((depth(k, seen | {node}) for k in kids), default=0)

    if depth(doc["root"], frozenset()) > MAX_DEPTH:
        raise MeshError("surface.malformed", "too deep")
    reachable = set()
    stack = [doc["root"]]
    while stack:
        node = stack.pop()
        reachable.add(node)
        stack.extend(components[node]["children"])
    if reachable != set(components):
        raise MeshError("surface.malformed", "orphan components")


def _remove_subtree(doc: dict[str, Any], cid: str) -> None:
    for child in list(doc["components"][cid]["children"]):
        _remove_subtree(doc, child)
    del doc["components"][cid]


def apply_patch(
    doc: dict[str, Any], ops: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[int]]:
    result = copy.deepcopy(doc)
    skipped: list[int] = []
    for n, op in enumerate(ops):
        kind = op.get("op")
        comps = result["components"]
        if kind in ("set", "unset"):
            if op["id"] not in comps:
                skipped.append(n)
            elif kind == "set":
                comps[op["id"]]["props"][op["prop"]] = op["value"]
            else:
                comps[op["id"]]["props"].pop(op["prop"], None)
        elif kind == "add":
            parent = op["parent"]
            component = op["component"]
            if parent not in comps or component["id"] in comps:
                skipped.append(n)
                continue
            index = op["index"]
            kids = comps[parent]["children"]
            kids.insert(
                len(kids) if index is None else min(index, len(kids)), component["id"]
            )
            comps[component["id"]] = component
        elif kind == "remove":
            cid = op["id"]
            if cid not in comps or cid == result["root"]:
                skipped.append(n)
                continue
            for comp in comps.values():
                if cid in comp["children"]:
                    comp["children"].remove(cid)
            _remove_subtree(result, cid)
        else:
            raise MeshError("surface.patch_invalid", f"unknown op {kind}")
    return result, skipped


def order_key(item: dict[str, Any]) -> tuple[int, int, str, int]:
    return (item["clock"]["ms"], item["clock"]["n"], item["feed"], item["seq"])


def _replay(
    events: list[dict[str, Any]],
    *,
    vocabulary: set[str],
    actionable: set[str],
    turn: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, int]]:
    doc: dict[str, Any] | None = None
    rejected: list[dict[str, Any]] = []
    used: dict[str, int] = {}
    for item in sorted(events, key=order_key):
        body = item["body"]
        if item["type"] == "ui.surface_put":
            try:
                validate_document(
                    body["document"],
                    vocabulary=vocabulary,
                    actionable=actionable,
                    turn=turn,
                )
            except MeshError as exc:
                rejected.append(
                    {"seq": item["seq"], "feed": item["feed"], "code": exc.code}
                )
                continue
            doc = copy.deepcopy(body["document"])
        elif item["type"] == "ui.patch" and doc is not None:
            patched, skipped = apply_patch(doc, body["ops"])
            try:
                validate_document(
                    patched, vocabulary=vocabulary, actionable=actionable, turn=turn
                )
            except MeshError as exc:
                rejected.append(
                    {"seq": item["seq"], "feed": item["feed"], "code": exc.code}
                )
                continue
            doc = patched
        elif item["type"] == "ui.action" and doc is not None:
            capability = body["capability"]
            try:
                validate_action(doc, body, used.get(capability, 0))
            except MeshError:
                continue
            used[capability] = used.get(capability, 0) + 1
    return doc, rejected, used


def fold(
    events: list[dict[str, Any]],
    *,
    vocabulary: set[str],
    actionable: set[str],
    turn: dict[str, Any],
) -> dict[str, Any]:
    doc, rejected, _ = _replay(
        events, vocabulary=vocabulary, actionable=actionable, turn=turn
    )
    return {"document": doc, "rejected": rejected}


def act(
    events: list[dict[str, Any]],
    action: dict[str, Any],
    *,
    vocabulary: set[str],
    actionable: set[str],
    turn: dict[str, Any],
) -> dict[str, Any]:
    doc, rejected, used = _replay(
        events, vocabulary=vocabulary, actionable=actionable, turn=turn
    )
    if doc is None:
        raise MeshError(
            rejected[0]["code"] if rejected else "surface.unknown_surface",
            "no accepted document",
        )
    return validate_action(doc, action, used.get(action["capability"], 0))


def validate_action(
    doc: dict[str, Any], action: dict[str, Any], used: int
) -> dict[str, Any]:
    comp = doc["components"].get(action["component"])
    if comp is None or "action" not in comp["props"]:
        raise MeshError("surface.unknown_component", str(action["component"]))
    cap_id = comp["props"]["action"]["capability"]
    if action["capability"] != cap_id:
        raise MeshError("surface.unknown_capability", str(action["capability"]))
    capability = doc["capabilities"][cap_id]
    args = action["args"]
    for key, value in capability["args"].items():
        if args.get(key) != value:
            raise MeshError("surface.action_args", f"{key} is pinned by the capability")
    extra = set(args) - set(capability["args"])
    if not extra <= set(capability["free"]):
        raise MeshError("surface.action_args", "argument not declared free")
    if used >= capability["max_uses"]:
        raise MeshError("surface.capability_exhausted", cap_id)
    return {"tool": capability["tool"], "consent": capability["consent"], "args": args}


def resolve_binding(binding: dict[str, Any], events: dict[str, dict[str, Any]]) -> Any:
    event = events.get(binding["event"])
    if event is None:
        raise MeshError("surface.bind_unknown_event", binding["event"])
    if event["type"] != "chat.tool_result":
        raise MeshError("surface.bind_not_tool_result", binding["event"])
    value: Any = event["body"]
    path = binding["path"]
    if path != "" and not path.startswith("/"):
        raise MeshError("surface.bind_path", path)
    for part in [] if path == "" else path[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        try:
            value = value[int(part)] if isinstance(value, list) else value[part]
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise MeshError("surface.bind_path", path) from exc
    if not _scalar(value):
        raise MeshError("surface.bind_not_scalar", path)
    return value
