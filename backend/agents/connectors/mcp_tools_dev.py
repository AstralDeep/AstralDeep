"""Developer connector tools: AST-based Python code review (with a regex fallback for
other languages) and markdown-section-aware constitution critique.
"""

import ast
import logging
import re
from typing import Dict, Any, List, Tuple

from astralprims import (
    Alert, Collapsible, Text, Container, Divider,
    create_ui_response,
)

logger = logging.getLogger("Connectors.Dev")


_CODE_REVIEW_METADATA = {
    "name": "code_review",
    "description": "Perform an automated code review with structured findings: issues, suggestions, and security notes.",
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Code snippet to review"},
            "language": {"type": "string", "description": "Programming language"},
            "focus": {"type": "string", "enum": ["general", "security", "performance", "style"], "description": "Review focus area"},
        },
        "required": ["code"],
    },
}


_DANGEROUS_PY_CALLS = {"eval", "exec", "compile"}
_ADVISORY_PY_IMPORTS = {"pickle", "marshal", "subprocess", "shelve"}


class _PyAuditor(ast.NodeVisitor):
    def __init__(self):
        self.security: List[str] = []
        self.issues: List[str] = []
        self.suggestions: List[str] = []

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name in _DANGEROUS_PY_CALLS:
            self.security.append(
                f"Line {node.lineno}: {name}() call — code-injection risk if any argument is user-supplied."
            )
        if isinstance(func, ast.Attribute) and func.attr == "system":
            value = func.value
            if isinstance(value, ast.Name) and value.id == "os":
                self.security.append(
                    f"Line {node.lineno}: os.system() — shell execution; prefer subprocess.run([...], shell=False)."
                )
        self.generic_visit(node)

    def _check_import_names(self, names: List[ast.alias], lineno: int) -> None:
        for alias in names:
            mod = (alias.name or "").split(".", 1)[0]
            if mod in _ADVISORY_PY_IMPORTS:
                self.security.append(
                    f"Line {lineno}: imports '{alias.name}' — review usage (unpickling untrusted data / shell-out are common pitfalls)."
                )

    def visit_Import(self, node: ast.Import) -> None:
        self._check_import_names(node.names, node.lineno)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        mod = (node.module or "").split(".", 1)[0]
        if mod in _ADVISORY_PY_IMPORTS:
            self.security.append(
                f"Line {node.lineno}: imports from '{node.module}' — review usage."
            )
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            self.issues.append(
                f"Line {node.lineno}: bare 'except:' swallows every exception including KeyboardInterrupt — narrow the clause."
            )
        self.generic_visit(node)

    def _check_function(self, node) -> None:
        body_stmts = sum(1 for _ in ast.walk(node)) - 1
        if body_stmts > 120:
            self.suggestions.append(
                f"Line {node.lineno}: '{node.name}' is large ({body_stmts} AST nodes) — consider splitting."
            )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_function(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check_function(node)
        self.generic_visit(node)


def _regex_pass(code: str, language: str, focus: str) -> Tuple[List[str], List[str], List[str]]:
    issues: List[str] = []
    suggestions: List[str] = []
    security: List[str] = []

    lines = code.split("\n")
    if len(lines) > 200:
        issues.append(f"File is {len(lines)} lines. Consider splitting into modules.")
        suggestions.append("Break large files into focused modules (< 200 lines each).")
    if any(len(line) > 120 for line in lines):
        issues.append("Some lines exceed 120 characters.")
        suggestions.append("Wrap long lines at 100-120 characters for readability.")
    if "\t" in code:
        issues.append("Tab characters detected.")
        suggestions.append("Convert tabs to spaces (PEP 8 recommends 4 spaces).")

    lang = (language or "").lower()
    if lang in ("python", "py"):
        if "eval(" in code or "exec(" in code:
            security.append("eval()/exec() token detected — potential code injection risk.")
        if "password" in code.lower() or "secret" in code.lower() or "api_key" in code.lower():
            security.append("Hardcoded credentials detected. Use environment variables or a secret manager.")
    elif lang in ("javascript", "js", "typescript", "ts"):
        if "innerHTML" in code or "dangerouslySetInnerHTML" in code:
            security.append("Unsafe HTML insertion detected. Use textContent or sanitize with DOMPurify.")
        if "localStorage" in code:
            security.append("localStorage usage — never store sensitive tokens or credentials.")

    if focus == "performance" and "for " in code and ("list.append" in code or "push(" in code):
        suggestions.append("Consider list comprehensions or array methods for performance on large datasets.")

    return issues, suggestions, security


def handle_code_review(args: Dict[str, Any]) -> Dict[str, Any]:
    code = args.get("code", "")
    language = args.get("language", "unknown")
    focus = args.get("focus", "general")

    issues, suggestions, security_notes = _regex_pass(code, language, focus)

    if language and language.lower() in ("python", "py"):
        try:
            tree = ast.parse(code)
            auditor = _PyAuditor()
            auditor.visit(tree)
            issues.extend(auditor.issues)
            suggestions.extend(auditor.suggestions)
            security_notes.extend(auditor.security)
        except SyntaxError as e:
            issues.append(f"Python parse error: {e.msg} (line {e.lineno})")

    def _dedupe(items: List[str]) -> List[str]:
        seen, out = set(), []
        for item in items:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out

    issues = _dedupe(issues)
    suggestions = _dedupe(suggestions)
    security_notes = _dedupe(security_notes)

    components = [
        Text(
            content=f"Code Review — {language}" + (f" (focus: {focus})" if focus != "general" else ""),
            variant="h2",
        ),
    ]
    if issues:
        for issue in issues:
            components.append(Alert(variant="warning", title="Issue", message=issue))
    else:
        components.append(Alert(
            variant="success",
            title="No issues found",
            message="No structural issues detected.",
        ))

    if suggestions:
        components.append(Collapsible(
            title="Suggestions",
            content=[Container(children=[Text(content=f"• {s}") for s in suggestions])],
        ))
    if security_notes:
        components.append(Collapsible(
            title="Security Notes",
            content=[Container(children=[Text(content=note) for note in security_notes])],
        ))

    return create_ui_response(components)


_CONSTITUTION_METADATA = {
    "name": "constitution_critique",
    "description": "Review a specification document against constitution principles for spec-driven development.",
    "input_schema": {
        "type": "object",
        "properties": {
            "spec": {"type": "string", "description": "The specification document content to review"},
            "spec_title": {"type": "string", "description": "Title of the specification"},
            "constitution_version": {"type": "string", "description": "Version of the constitution to check against"},
        },
        "required": ["spec", "spec_title"],
    },
}

CONSTITUTION_PRINCIPLES = [
    ("I - Patient Safety First", "All generated components must be safe and not cause harm."),
    ("II - Data Privacy by Default", "No PHI/PII in logs, URLs, or client-side storage."),
    ("III - Transparent AI", "AI-generated content must be clearly labeled as such."),
    ("IV - Audit Trail Integrity", "All system actions must be logged and immutable."),
    ("V - Dependency Minimization", "No new third-party libraries without explicit review."),
    ("VI - Secure by Default", "Security controls must be enabled by default, not opt-in."),
    ("VII - User Agency", "Users retain final control over AI-generated actions."),
    ("VIII - Accessibility First", "Components must be keyboard-navigable and screen-reader accessible."),
    ("IX - Database Integrity", "All schema changes must be migration-tracked and reversible."),
    ("X - Code Quality", "Code must be typed, tested, and documented."),
]

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


def _split_sections(spec: str) -> Dict[str, str]:
    headings = list(_HEADING_RE.finditer(spec))
    if not headings:
        return {"_full_": spec.lower()}

    sections: Dict[str, str] = {}
    for i, match in enumerate(headings):
        heading = match.group(2).strip().lower()
        body_start = match.end()
        body_end = headings[i + 1].start() if i + 1 < len(headings) else len(spec)
        sections[heading] = spec[body_start:body_end].lower()
    return sections


def _spec_mentions(sections: Dict[str, str], *needles: str) -> bool:
    for heading, body in sections.items():
        if any(n in heading for n in needles):
            return True
        if any(n in body for n in needles):
            return True
    return False


def handle_constitution_critique(args: Dict[str, Any]) -> Dict[str, Any]:
    spec = args.get("spec", "")
    spec_title = args.get("spec_title", "Untitled Spec")
    constitution_version = args.get("constitution_version", "1.1.0")

    sections = _split_sections(spec)
    findings: List[Tuple[str, str, str]] = []

    if not _spec_mentions(sections, "test", "testing", "pytest", "jest"):
        findings.append(("X", "warn", "No test plan mentioned. Constitution X requires tests."))
    if not _spec_mentions(sections, "privacy", "pii", "phi", "redact"):
        findings.append(("II", "warn", "No mention of data privacy/PII/PHI handling. Review Constitution II."))
    if not _spec_mentions(sections, "audit"):
        findings.append(("IV", "info", "No audit trail considerations. Review Constitution IV."))
    if _spec_mentions(sections, "library", "package", "dependency", "third-party"):
        findings.append(("V", "info", "External dependencies mentioned. Ensure Constitution V compliance."))
    if _spec_mentions(sections, "database", "schema", "table") and not _spec_mentions(sections, "migration"):
        findings.append(("IX", "warn", "Database changes planned but no migration strategy mentioned. Constitution IX."))
    if _spec_mentions(sections, "ai", "llm", "model") and not _spec_mentions(sections, "label", "disclosure", "watermark", "tagged"):
        findings.append(("III", "info", "AI/LLM usage mentioned without an explicit labeling/disclosure strategy. Review Constitution III."))

    components = [
        Text(content=f"Constitution Critique: {spec_title}", variant="h2"),
        Text(
            content=f"Constitution v{constitution_version} — {len(CONSTITUTION_PRINCIPLES)} principles checked across {len(sections)} section(s)",
            variant="caption",
        ),
        Divider(),
    ]

    if findings:
        for principle, level, note in findings:
            variant = "warning" if level == "warn" else "info"
            components.append(Alert(variant=variant, title=f"Principle {principle}", message=note))
    else:
        components.append(Alert(
            variant="success",
            title="No issues found",
            message="Spec passes basic constitutional checks.",
        ))

    components.append(Collapsible(
        title="Constitution Reference",
        content=[Container(children=[Text(content=f"{name}: {desc}") for name, desc in CONSTITUTION_PRINCIPLES])],
    ))

    return create_ui_response(components)


DEV_TOOL_REGISTRY = {
    "code_review": {"function": handle_code_review, **_CODE_REVIEW_METADATA},
    "constitution_critique": {"function": handle_constitution_critique, **_CONSTITUTION_METADATA},
}
