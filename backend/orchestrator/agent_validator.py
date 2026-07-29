"""
Agent Spec Validator — validates generated mcp_tools.py against the agent constitution.

Two validators, and the difference is the whole security story:

* ``validate`` EXECUTES the code under test (module import + a call to every
  tool with synthesized args). That is acceptable ONLY for the server-hosted 027
  path, where the same code is about to be ``Popen``'d on this host anyway.
* ``validate_static`` NEVER imports, execs, compiles-and-runs, or otherwise
  evaluates the code. It is pure ``ast`` inspection: registry shape, return
  format, and an IMPORT ALLOWLIST. This is the ONLY validator a BYO agent's
  code may see (058 G1/SC-002) — user-authored code never runs centrally, so
  the orchestrator (which holds DB credentials and Fernet keys) can never be
  the thing that runs it. Runtime behavior is the desktop host's business: the
  agent either registers on the owner's machine or it doesn't.

The import allowlist is also a HOST-COMPATIBILITY gate: the desktop host ships
only the standard library plus ``astralprims``, so a bundle that imports
``requests`` would die at import on the user's machine with no ``register_agent``
frame — surfacing only as the host's silence timeout. Refuse it at generation.
"""
import ast
import importlib.util
import logging
import os
import sys
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, Any, Optional, List, Set, Tuple

from orchestrator.agent_spec import VALID_COMPONENT_TYPES, PRIMITIVES_SPEC

logger = logging.getLogger("AgentValidator")

#: Everything a BYO bundle is allowed to import: the standard library (which the
#: host's interpreter always has) plus astralprims (the one client-side
#: third-party dependency, Constitution V carve-out).
BYO_EXTRA_ALLOWED_IMPORTS: Set[str] = {"astralprims"}


def byo_allowed_modules() -> Set[str]:
    """The BYO import allowlist: stdlib ∪ {astralprims}."""
    return set(getattr(sys, "stdlib_module_names", set())) | BYO_EXTRA_ALLOWED_IMPORTS


#: Reported by :func:`disallowed_imports` for a dynamic import whose module name
#: is not a literal, so the allowlist cannot be decided statically at all.
UNRESOLVABLE_DYNAMIC_IMPORT = "<computed at runtime>"


def _dynamic_import_arg(node: ast.Call) -> Optional[ast.expr]:
    """The module-name argument of ``__import__(...)``/``importlib.import_module(...)``.

    Returns ``None`` for any other call. Matches the bare name (``__import__``,
    or ``import_module`` after ``from importlib import import_module``) and the
    attribute form (``importlib.import_module``), which is every spelling a
    flat 3-file bundle can reach.
    """
    func = node.func
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute):
        name = func.attr
    else:
        return None
    if name not in ("__import__", "import_module"):
        return None
    if node.args:
        return node.args[0]
    for kw in node.keywords:
        if kw.arg in ("name", "module"):
            return kw.value
    return None


def disallowed_imports(code: str) -> List[str]:
    """Top-level module names imported by ``code`` that a BYO host cannot resolve.

    AST-only (never imports the module). A relative import is reported as
    ``.<name>``: the bundle is a flat 3-file directory with no package.

    Covers DYNAMIC imports too — ``__import__("requests")`` and
    ``importlib.import_module("requests")`` reach the host's interpreter exactly
    like a static ``import requests`` and die there the same way, but a
    statement-only walk never saw them. The cost of missing one is not a
    security hole (validation never executes the code, and ``__import__`` is
    separately flagged CRITICAL by ``CodeSecurityAnalyzer``, which aborts BYO
    generation) — it is that the author gets no generation-time feedback and
    instead watches the agent fail as an unexplained host silence-timeout.
    A non-literal module name is reported as
    :data:`UNRESOLVABLE_DYNAMIC_IMPORT`, since host compatibility cannot be
    decided statically in that case either.
    """
    allowed = byo_allowed_modules()
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return []          # the syntax error is reported by the caller
    bad: List[str] = []

    def _flag(name: str) -> None:
        if name and name not in bad:
            bad.append(name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = (alias.name or "").split(".")[0]
                if root and root not in allowed:
                    _flag(root)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                _flag("." * node.level + (node.module or ""))
                continue
            root = (node.module or "").split(".")[0]
            if root and root not in allowed:
                _flag(root)
        elif isinstance(node, ast.Call):
            arg = _dynamic_import_arg(node)
            if arg is None:
                continue
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                root = arg.value.split(".")[0]
                if root and root not in allowed:
                    _flag(root)
            else:
                _flag(UNRESOLVABLE_DYNAMIC_IMPORT)
    return bad


# Exceptions that indicate structural code bugs (not transient failures)
STRUCTURAL_EXCEPTIONS = (
    TypeError, NameError, AttributeError, KeyError, ImportError,
    ModuleNotFoundError, SyntaxError, IndentationError, UnboundLocalError,
)

# Exceptions that indicate network/external API failures (transient)
NETWORK_EXCEPTIONS = (ConnectionError, TimeoutError, OSError)

try:
    import requests
    NETWORK_EXCEPTIONS = NETWORK_EXCEPTIONS + (
        requests.exceptions.RequestException,
    )
except ImportError:
    pass

try:
    import httpx
    NETWORK_EXCEPTIONS = NETWORK_EXCEPTIONS + (
        httpx.HTTPError,
    )
except ImportError:
    pass


class ValidationSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class ValidationFinding:
    severity: str  # ValidationSeverity value
    category: str  # IMPORT, REGISTRY, EXECUTION, RETURN_FORMAT, COMPONENT
    message: str
    tool_name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationReport:
    passed: bool = True
    findings: List[ValidationFinding] = field(default_factory=list)
    tools_tested: int = 0
    tools_passed: int = 0
    tools: List[Dict[str, Any]] = field(default_factory=list)

    def add(self, severity: str, category: str, message: str,
            tool_name: str = None):
        self.findings.append(ValidationFinding(
            severity=severity, category=category,
            message=message, tool_name=tool_name,
        ))
        if severity == ValidationSeverity.ERROR:
            self.passed = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "tools_tested": self.tools_tested,
            "tools_passed": self.tools_passed,
            "findings": [f.to_dict() for f in self.findings],
            "tools": self.tools,
        }


def registry_from_source(code: str) -> Dict[str, Dict[str, Any]]:
    """The declared TOOL_REGISTRY of ``code``, read by AST (never executed).

    ``{tool_name: {"description", "input_schema", "scope", "_function_name"}}``;
    an unparseable/absent registry yields ``{}``. Callers use this to check what
    the generator ACTUALLY produced against what Analyze approved.
    """
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return {}
    registry, _ = AgentSpecValidator._static_registry(tree, ValidationReport())
    return registry or {}


class AgentSpecValidator:
    """Validates generated mcp_tools.py files against the agent constitution."""

    # ── Static (BYO) validation — NEVER executes the code under test ─────────

    def validate_static(self, code: str, slug: str = "") -> ValidationReport:
        """Validate BYO agent code WITHOUT running it (058 G1/SC-002).

        Pure ``ast`` inspection — no import, no exec, no compile-and-run. Checks:

        1. the file parses;
        2. every import resolves on the desktop host (stdlib ∪ astralprims) —
           this is a GATE, not a warning: an ``import requests`` bundle would
           die silently on the user's machine;
        3. ``TOOL_REGISTRY`` exists as a module-level dict literal, is non-empty,
           and every entry has ``function`` (a module-level def),
           ``description``, ``input_schema`` and ``scope``;
        4. every registered tool's body returns the ``_ui_components`` contract
           (a dict literal carrying the key, or ``create_ui_response(...)``).

        Tool *behavior* is deliberately NOT checked: it is the desktop host's
        business, and checking it would mean running the user's code here.
        """
        report = ValidationReport()

        try:
            tree = ast.parse(code or "")
        except SyntaxError as e:
            report.add(ValidationSeverity.ERROR, "IMPORT",
                       f"Syntax error prevents parsing: {e}")
            return report

        # (2) Import allowlist — the host ships stdlib + astralprims, nothing else.
        for module in disallowed_imports(code):
            if module == UNRESOLVABLE_DYNAMIC_IMPORT:
                report.add(ValidationSeverity.ERROR, "IMPORT",
                           "Imports a module whose name is computed at runtime, so "
                           "host compatibility cannot be checked before delivery. "
                           "Import by literal name — a user agent may import ONLY "
                           "the Python standard library and 'astralprims'.")
                continue
            report.add(ValidationSeverity.ERROR, "IMPORT",
                       f"Imports '{module}', which the desktop host does not have. "
                       "A user agent may import ONLY the Python standard library "
                       "and 'astralprims'.")

        self._validate_imports(code, report)   # astralprims-usage WARNING (shared)

        # (3) Registry shape.
        registry, functions = self._static_registry(tree, report)
        if registry is None:
            return report

        for tool_name, entry in registry.items():
            report.tools_tested += 1
            fn_name = entry.get("_function_name")
            ok = True

            if not fn_name:
                report.add(ValidationSeverity.ERROR, "REGISTRY",
                           "Missing 'function' key (must name a module-level "
                           "function defined in this file).", tool_name=tool_name)
                ok = False
            elif fn_name not in functions:
                report.add(ValidationSeverity.ERROR, "REGISTRY",
                           f"'function' names '{fn_name}', which is not a function "
                           "defined in this file.", tool_name=tool_name)
                ok = False

            if not entry.get("description"):
                report.add(ValidationSeverity.WARNING, "REGISTRY",
                           "Missing 'description' key.", tool_name=tool_name)
            if not isinstance(entry.get("input_schema"), dict):
                report.add(ValidationSeverity.WARNING, "REGISTRY",
                           "Missing or non-object 'input_schema' key.", tool_name=tool_name)
            if not isinstance(entry.get("scope"), str) or not entry.get("scope"):
                report.add(ValidationSeverity.WARNING, "REGISTRY",
                           "Missing 'scope' key (defaults to tools:read).",
                           tool_name=tool_name)

            # (4) Return contract — statically, from the function's own body.
            if ok and not self._returns_ui_contract(functions[fn_name]):
                report.add(ValidationSeverity.ERROR, "RETURN_FORMAT",
                           "The function never returns the required shape: a dict "
                           "with '_ui_components' (and '_data'), or "
                           "create_ui_response([...]).", tool_name=tool_name)
                ok = False

            schema = entry.get("input_schema") if isinstance(entry.get("input_schema"), dict) else {}
            props = schema.get("properties") or {}
            required = schema.get("required") or []
            params = [
                {"name": pname, "type": (pinfo or {}).get("type", "any"),
                 "description": (pinfo or {}).get("description", ""),
                 "required": pname in required}
                for pname, pinfo in props.items() if isinstance(pinfo, dict)
            ]
            report.tools.append({
                "name": tool_name,
                "description": entry.get("description") or "",
                "scope": entry.get("scope") or "tools:read",
                "parameters": params,
            })
            if ok:
                report.tools_passed += 1

        return report

    @staticmethod
    def _static_registry(tree: ast.Module, report: ValidationReport
                         ) -> Tuple[Optional[Dict[str, Dict[str, Any]]],
                                    Dict[str, ast.AST]]:
        """Extract TOOL_REGISTRY + the module-level function defs, via AST only.

        Each returned entry is ``{"_function_name", "description", "input_schema",
        "scope"}``; a non-literal value (e.g. a computed schema) reads as absent
        rather than being evaluated.
        """
        functions: Dict[str, ast.AST] = {
            n.name: n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        node = None
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "TOOL_REGISTRY"
                    for t in stmt.targets):
                node = stmt.value
        if node is None:
            report.add(ValidationSeverity.ERROR, "REGISTRY",
                       "TOOL_REGISTRY not found in mcp_tools.py. The file must "
                       "define a module-level TOOL_REGISTRY dict.")
            return None, functions
        if not isinstance(node, ast.Dict):
            report.add(ValidationSeverity.ERROR, "REGISTRY",
                       "TOOL_REGISTRY must be a dict literal.")
            return None, functions
        if not node.keys:
            report.add(ValidationSeverity.ERROR, "REGISTRY",
                       "TOOL_REGISTRY is empty — no tools defined.")
            return None, functions

        registry: Dict[str, Dict[str, Any]] = {}
        for key_node, val_node in zip(node.keys, node.values):
            if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
                report.add(ValidationSeverity.ERROR, "REGISTRY",
                           "TOOL_REGISTRY keys must be string literals (tool names).")
                continue
            tool_name = key_node.value
            if not isinstance(val_node, ast.Dict):
                report.add(ValidationSeverity.ERROR, "REGISTRY",
                           "TOOL_REGISTRY entry must be a dict literal.",
                           tool_name=tool_name)
                continue
            entry: Dict[str, Any] = {}
            for k, v in zip(val_node.keys, val_node.values):
                if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                    continue
                if k.value == "function":
                    if isinstance(v, ast.Name):
                        entry["_function_name"] = v.id
                    continue
                try:
                    entry[k.value] = ast.literal_eval(v)
                except (ValueError, SyntaxError, TypeError):
                    continue      # non-literal: read as absent, never evaluated
            registry[tool_name] = entry

        if not registry:
            report.add(ValidationSeverity.ERROR, "REGISTRY",
                       "TOOL_REGISTRY has no usable tool entries.")
            return None, functions
        return registry, functions

    @staticmethod
    def _returns_ui_contract(fn: ast.AST) -> bool:
        """True when the function's body can return the ``_ui_components`` shape."""
        for node in ast.walk(fn):
            # Any dict literal in the body carrying the key — covers both a
            # direct `return {...}` and a payload assembled into a local first.
            if isinstance(node, ast.Dict):
                for k in node.keys:
                    if isinstance(k, ast.Constant) and k.value == "_ui_components":
                        return True
            # create_ui_response([...]) builds the same shape.
            if isinstance(node, ast.Call):
                fname = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if fname == "create_ui_response":
                    return True
        return False

    def validate(self, code: str, slug: str, agents_dir: str) -> ValidationReport:
        """Run the full validation pipeline on generated tool code.

        Args:
            code: The mcp_tools.py source code
            slug: The agent slug (directory name)
            agents_dir: Path to backend/agents/

        Returns:
            ValidationReport with findings
        """
        report = ValidationReport()

        # Step 1: Check imports
        self._validate_imports(code, report)

        # Step 2-3: Load and validate TOOL_REGISTRY
        registry = self._load_registry(code, slug, agents_dir, report)
        if registry is None:
            return report

        # Capture tool metadata for the report
        for tool_name, tool_info in registry.items():
            schema = tool_info.get("input_schema", {})
            props = schema.get("properties", {})
            required = schema.get("required", [])
            params = []
            for pname, pinfo in props.items():
                if isinstance(pinfo, dict):
                    params.append({
                        "name": pname,
                        "type": pinfo.get("type", "any"),
                        "description": pinfo.get("description", ""),
                        "required": pname in required,
                    })
            report.tools.append({
                "name": tool_name,
                "description": tool_info.get("description", ""),
                "scope": tool_info.get("scope", "tools:read"),
                "parameters": params,
            })

        # Step 4-7: Validate each tool
        for tool_name, tool_info in registry.items():
            self._validate_tool(tool_name, tool_info, report)

        return report

    def _validate_imports(self, code: str, report: ValidationReport):
        """Check that code imports primitive classes from astralprims."""
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            report.add(ValidationSeverity.ERROR, "IMPORT",
                       f"Syntax error prevents parsing: {e}")
            return

        has_primitives_import = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "astralprims" in module or "primitives" in module:
                    has_primitives_import = True
                    break

        if not has_primitives_import:
            report.add(ValidationSeverity.WARNING, "IMPORT",
                       "Code does not import from astralprims. "
                       "Tools should use primitive classes (Card, MetricCard, etc.) "
                       "and call .to_dict() instead of constructing raw dicts.")

    def _load_registry(self, code: str, slug: str, agents_dir: str,
                       report: ValidationReport) -> Optional[Dict]:
        """Dynamically import the module and extract TOOL_REGISTRY."""
        module_path = os.path.join(agents_dir, slug, "mcp_tools.py")

        if not os.path.exists(module_path):
            report.add(ValidationSeverity.ERROR, "REGISTRY",
                       f"mcp_tools.py not found at {module_path}")
            return None

        # Ensure backend is on sys.path for shared imports
        backend_dir = os.path.abspath(os.path.join(agents_dir, '..'))
        original_path = sys.path.copy()
        if backend_dir not in sys.path:
            sys.path.insert(0, backend_dir)

        try:
            # Create a unique module name to avoid caching issues
            module_name = f"_validator_{slug}_{id(report)}"
            spec = importlib.util.spec_from_file_location(module_name, module_path)
            if spec is None or spec.loader is None:
                report.add(ValidationSeverity.ERROR, "REGISTRY",
                           "Failed to create module spec for mcp_tools.py")
                return None

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            registry = getattr(module, "TOOL_REGISTRY", None)
            if registry is None:
                report.add(ValidationSeverity.ERROR, "REGISTRY",
                           "TOOL_REGISTRY not found in mcp_tools.py. "
                           "The file must export a TOOL_REGISTRY dict.")
                return None

            if not isinstance(registry, dict):
                report.add(ValidationSeverity.ERROR, "REGISTRY",
                           f"TOOL_REGISTRY is {type(registry).__name__}, expected dict.")
                return None

            if not registry:
                report.add(ValidationSeverity.ERROR, "REGISTRY",
                           "TOOL_REGISTRY is empty — no tools defined.")
                return None

            return registry

        except STRUCTURAL_EXCEPTIONS as e:
            report.add(ValidationSeverity.ERROR, "REGISTRY",
                       f"Failed to import mcp_tools.py: {type(e).__name__}: {e}")
            return None
        except NETWORK_EXCEPTIONS as e:
            report.add(ValidationSeverity.WARNING, "REGISTRY",
                       f"Module import triggered network call that failed: {e}. "
                       "Avoid making network calls at module level.")
            return None
        except Exception as e:
            report.add(ValidationSeverity.ERROR, "REGISTRY",
                       f"Unexpected error loading mcp_tools.py: {type(e).__name__}: {e}")
            return None
        finally:
            sys.path = original_path

    def _validate_tool(self, tool_name: str, tool_info: Dict,
                       report: ValidationReport):
        """Validate a single tool entry: structure, execution, and output."""
        report.tools_tested += 1
        tool_passed = True

        # Check required keys
        if "function" not in tool_info:
            report.add(ValidationSeverity.ERROR, "REGISTRY",
                       "Missing 'function' key.", tool_name=tool_name)
            return

        if not callable(tool_info["function"]):
            report.add(ValidationSeverity.ERROR, "REGISTRY",
                       "'function' is not callable.", tool_name=tool_name)
            return

        if "description" not in tool_info:
            report.add(ValidationSeverity.WARNING, "REGISTRY",
                       "Missing 'description' key.", tool_name=tool_name)

        if "input_schema" not in tool_info:
            report.add(ValidationSeverity.WARNING, "REGISTRY",
                       "Missing 'input_schema' key.", tool_name=tool_name)

        # Generate sample inputs and call the tool
        schema = tool_info.get("input_schema", {"type": "object", "properties": {}})
        sample_inputs = self._generate_sample_inputs(schema)

        try:
            result = tool_info["function"](**sample_inputs)
        except NETWORK_EXCEPTIONS as e:
            report.add(ValidationSeverity.WARNING, "EXECUTION",
                       f"Network error during execution (expected for API-dependent tools): "
                       f"{type(e).__name__}: {e}",
                       tool_name=tool_name)
            # Can't validate output, but the tool structure may be fine
            # Do a static check instead
            self._static_check_return_format(tool_info["function"], tool_name, report)
            report.tools_passed += 1
            return
        except STRUCTURAL_EXCEPTIONS as e:
            report.add(ValidationSeverity.ERROR, "EXECUTION",
                       f"Structural error: {type(e).__name__}: {e}",
                       tool_name=tool_name)
            return
        except Exception as e:
            report.add(ValidationSeverity.WARNING, "EXECUTION",
                       f"Execution error: {type(e).__name__}: {e}",
                       tool_name=tool_name)
            self._static_check_return_format(tool_info["function"], tool_name, report)
            report.tools_passed += 1
            return

        # Validate return format
        if not isinstance(result, dict):
            report.add(ValidationSeverity.ERROR, "RETURN_FORMAT",
                       f"Tool returned {type(result).__name__}, expected dict with "
                       f"'_ui_components' and '_data' keys.",
                       tool_name=tool_name)
            return

        if "_ui_components" not in result:
            report.add(ValidationSeverity.ERROR, "RETURN_FORMAT",
                       f"Return dict missing '_ui_components' key. "
                       f"Keys found: {list(result.keys())}",
                       tool_name=tool_name)
            tool_passed = False

        ui_comps = result.get("_ui_components")
        if ui_comps is not None:
            if not isinstance(ui_comps, list):
                report.add(ValidationSeverity.ERROR, "RETURN_FORMAT",
                           f"'_ui_components' is {type(ui_comps).__name__}, expected list.",
                           tool_name=tool_name)
                tool_passed = False
            elif len(ui_comps) == 0:
                report.add(ValidationSeverity.ERROR, "RETURN_FORMAT",
                           "'_ui_components' is an empty list. "
                           "Tools must return at least one UI component.",
                           tool_name=tool_name)
                tool_passed = False
            else:
                # Validate each component
                for i, comp in enumerate(ui_comps):
                    if not self._validate_component(comp, tool_name, i, report):
                        tool_passed = False

        if "_data" not in result:
            report.add(ValidationSeverity.WARNING, "RETURN_FORMAT",
                       "Return dict missing '_data' key (recommended for LLM context).",
                       tool_name=tool_name)

        if tool_passed:
            report.tools_passed += 1

    def _validate_component(self, comp: Any, tool_name: str, index: int,
                            report: ValidationReport) -> bool:
        """Validate a single component dict. Returns True if valid."""
        if not isinstance(comp, dict):
            report.add(ValidationSeverity.ERROR, "COMPONENT",
                       f"Component [{index}] is {type(comp).__name__}, expected dict. "
                       f"Did you forget to call .to_json()?",
                       tool_name=tool_name)
            return False

        comp_type = comp.get("type")
        if not comp_type:
            report.add(ValidationSeverity.ERROR, "COMPONENT",
                       f"Component [{index}] missing 'type' field.",
                       tool_name=tool_name)
            return False

        if comp_type not in VALID_COMPONENT_TYPES:
            report.add(ValidationSeverity.ERROR, "COMPONENT",
                       f"Component [{index}] has unknown type '{comp_type}'. "
                       f"Valid types: {sorted(VALID_COMPONENT_TYPES)}",
                       tool_name=tool_name)
            return False

        # Validate field names against the spec
        spec_fields = PRIMITIVES_SPEC.get(comp_type, {}).get("fields", {})
        for key in comp:
            if key not in spec_fields and key not in ("type", "id", "style"):
                report.add(ValidationSeverity.WARNING, "COMPONENT",
                           f"Component [{index}] (type='{comp_type}') has unknown "
                           f"field '{key}'. Expected fields: {list(spec_fields.keys())}",
                           tool_name=tool_name)

        # Check for common mistakes
        if comp_type == "card" and "children" in comp and "content" not in comp:
            report.add(ValidationSeverity.ERROR, "COMPONENT",
                       f"Card component [{index}] uses 'children' but Card expects 'content'. "
                       f"Change 'children' to 'content'.",
                       tool_name=tool_name)
            return False

        # Recursively validate nested components
        valid = True
        for container_field in ("content", "children"):
            nested = comp.get(container_field)
            if isinstance(nested, list):
                for j, child in enumerate(nested):
                    if isinstance(child, dict) and "type" in child:
                        if not self._validate_component(child, tool_name,
                                                         f"{index}.{container_field}[{j}]",
                                                         report):
                            valid = False

        return valid

    def _generate_sample_inputs(self, schema: Dict) -> Dict[str, Any]:
        """Generate sample inputs from a JSON schema."""
        props = schema.get("properties", {})
        set(schema.get("required", []))
        sample = {}

        for name, prop in props.items():
            prop_type = prop.get("type", "string")
            enum = prop.get("enum")

            if enum:
                sample[name] = enum[0]
            elif prop_type == "string":
                sample[name] = prop.get("default", "sample")
            elif prop_type in ("number", "integer"):
                sample[name] = prop.get("default", 42)
            elif prop_type == "boolean":
                sample[name] = prop.get("default", True)
            elif prop_type == "array":
                sample[name] = prop.get("default", [])
            elif prop_type == "object":
                sample[name] = prop.get("default", {})
            else:
                sample[name] = "sample"

        return sample

    def _static_check_return_format(self, func, tool_name: str,
                                     report: ValidationReport):
        """When a tool can't be executed, do a static AST check for return format."""
        try:
            import inspect
            source = inspect.getsource(func)
            if "_ui_components" not in source:
                report.add(ValidationSeverity.ERROR, "RETURN_FORMAT",
                           "Source code does not contain '_ui_components'. "
                           "Tool likely doesn't return the required format.",
                           tool_name=tool_name)
            if ".to_json()" not in source and "to_json" not in source:
                report.add(ValidationSeverity.WARNING, "RETURN_FORMAT",
                           "Source code doesn't call '.to_json()'. "
                           "Components may not be serialized correctly.",
                           tool_name=tool_name)
        except (OSError, TypeError):
            pass  # Can't inspect source, skip static check
