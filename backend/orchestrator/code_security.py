"""AST- and regex-based static analyzer that scores generated agent code for dangerous
imports, calls, and obfuscation before it can be written or executed; used by
agent_lifecycle.py and agentic_creation.py to gate codegen.
"""

import ast
import re
import logging
from dataclasses import dataclass
from enum import Enum
from typing import List, Dict, Optional, Any

logger = logging.getLogger("CodeSecurity")


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class SecurityFinding:
    severity: Severity
    category: str
    message: str
    line: Optional[int] = None
    code_snippet: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity.value,
            "category": self.category,
            "message": self.message,
            "line": self.line,
            "code_snippet": self.code_snippet,
        }


@dataclass
class SecurityReport:
    passed: bool
    findings: List[SecurityFinding]
    max_severity: Optional[Severity] = None
    recommendation: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "findings": [f.to_dict() for f in self.findings],
            "max_severity": self.max_severity.value if self.max_severity else None,
            "recommendation": self.recommendation,
        }


EXECUTION_BLOCKING_SEVERITIES = (Severity.CRITICAL, Severity.HIGH)


def blocks_execution(report: SecurityReport) -> bool:
    return report.max_severity in EXECUTION_BLOCKING_SEVERITIES


BLOCKED_MODULES = {
    "subprocess", "shlex", "ctypes", "cffi", "code", "codeop",
    "compileall", "py_compile", "importlib", "runpy",
    "signal", "resource", "pty", "termios", "fcntl",
    "multiprocessing", "concurrent.futures",
}

PARTIAL_MODULES = {
    "os": {
        "allowed": {"os.path", "os.sep", "os.linesep", "os.getcwd", "os.path.join",
                     "os.path.exists", "os.path.dirname", "os.path.basename",
                     "os.path.abspath", "os.path.splitext"},
        "blocked_attrs": {"system", "popen", "exec", "execl", "execle", "execlp",
                          "execv", "execve", "execvp", "execvpe", "spawn", "spawnl",
                          "kill", "killpg", "fork", "forkpty", "environ", "getenv",
                          "putenv", "unsetenv", "remove", "unlink", "rmdir",
                          "removedirs", "rename", "renames", "replace", "makedirs",
                          "mkdir", "chmod", "chown", "chroot"},
    },
    "shutil": {
        "allowed": set(),
        "blocked_attrs": {"rmtree", "move", "copy", "copy2", "copytree", "disk_usage"},
    },
}

DANGEROUS_CALLS = {
    "eval": Severity.CRITICAL,
    "exec": Severity.CRITICAL,
    "compile": Severity.CRITICAL,
    "__import__": Severity.CRITICAL,
    "globals": Severity.HIGH,
    "locals": Severity.HIGH,
    "getattr": Severity.MEDIUM,
    "setattr": Severity.HIGH,
    "delattr": Severity.HIGH,
    "open": Severity.MEDIUM,
    "input": Severity.MEDIUM,
}

OBFUSCATION_PATTERNS = [
    (r"base64\.\s*b64decode\s*\(.*?\)\s*\)\s*$", Severity.CRITICAL,
     "Base64-decoded execution detected"),
    (r"chr\s*\(\s*\d+\s*\)\s*\+\s*chr", Severity.HIGH,
     "Character code concatenation — possible obfuscation"),
    # 9+ hex escapes only — 8 false-flagged real binary parsers
    (r"(?:\\x[0-9a-fA-F]{2}[^\n]{0,6}?){9,}", Severity.HIGH,
     "Hex-encoded string — possible obfuscation"),
    (r"(?:socket\.(?:bind|listen|connect))", Severity.CRITICAL,
     "Raw socket operation detected"),
    (r"(?:reverse.{0,10}shell|bind.{0,10}shell)", Severity.CRITICAL,
     "Shell pattern detected"),
    (r"pickle\.loads?\s*\(", Severity.CRITICAL,
     "Unsafe deserialization via pickle"),
    (r"marshal\.loads?\s*\(", Severity.CRITICAL,
     "Unsafe deserialization via marshal"),
    (r"yaml\.load\s*\((?!.*Loader\s*=\s*yaml\.SafeLoader)", Severity.HIGH,
     "Unsafe YAML loading without SafeLoader"),
    (r"__(?:subclasses|bases|mro|class)__", Severity.CRITICAL,
     "Python class hierarchy traversal — possible sandbox escape"),
]


class CodeSecurityAnalyzer:
    def __init__(self):
        self._compiled_patterns = [
            (re.compile(pat, re.IGNORECASE | re.MULTILINE), sev, msg)
            for pat, sev, msg in OBFUSCATION_PATTERNS
        ]

    def analyze(self, code: str, filename: str = "mcp_tools.py") -> SecurityReport:
        findings: List[SecurityFinding] = []

        try:
            tree = ast.parse(code)
            findings.extend(self._analyze_ast(tree, code))
        except SyntaxError as e:
            findings.append(SecurityFinding(
                severity=Severity.CRITICAL,
                category="SYNTAX_ERROR",
                message=f"Code has syntax errors and cannot be parsed: {e}",
                line=e.lineno,
            ))

        findings.extend(self._analyze_imports(code))

        findings.extend(self._analyze_patterns(code))

        max_severity = None
        for f in findings:
            if max_severity is None or self._severity_rank(f.severity) > self._severity_rank(max_severity):
                max_severity = f.severity

        if max_severity == Severity.CRITICAL:
            passed = False
            recommendation = "Code contains critical security issues and must not be executed."
        elif max_severity == Severity.HIGH:
            passed = False
            recommendation = "Code contains high-severity issues requiring admin review."
        else:
            passed = True
            recommendation = "Code passed security analysis." if not findings else \
                "Code passed with warnings — review recommended."

        report = SecurityReport(
            passed=passed,
            findings=findings,
            max_severity=max_severity,
            recommendation=recommendation,
        )

        if not passed:
            logger.warning(f"Security analysis FAILED for {filename}: "
                          f"{len(findings)} finding(s), max_severity={max_severity}")
        else:
            logger.info(f"Security analysis passed for {filename}: "
                       f"{len(findings)} finding(s)")

        return report

    def _severity_rank(self, severity: Severity) -> int:
        return {Severity.LOW: 1, Severity.MEDIUM: 2, Severity.HIGH: 3, Severity.CRITICAL: 4}[severity]

    def _analyze_ast(self, tree: ast.AST, source: str) -> List[SecurityFinding]:
        findings = []
        source_lines = source.split("\n")

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = self._get_call_name(node)
                if func_name in DANGEROUS_CALLS:
                    severity = DANGEROUS_CALLS[func_name]
                    snippet = source_lines[node.lineno - 1].strip() if node.lineno <= len(source_lines) else None
                    findings.append(SecurityFinding(
                        severity=severity,
                        category="DANGEROUS_CALL",
                        message=f"Dangerous function call: {func_name}()",
                        line=node.lineno,
                        code_snippet=snippet,
                    ))

                if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                    module = node.func.value.id
                    attr = node.func.attr
                    if module in PARTIAL_MODULES:
                        blocked_attrs = PARTIAL_MODULES[module].get("blocked_attrs", set())
                        if attr in blocked_attrs:
                            snippet = source_lines[node.lineno - 1].strip() if node.lineno <= len(source_lines) else None
                            findings.append(SecurityFinding(
                                severity=Severity.CRITICAL,
                                category="BLOCKED_FUNCTION",
                                message=f"Blocked function: {module}.{attr}()",
                                line=node.lineno,
                                code_snippet=snippet,
                            ))

            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                module = node.value.id
                attr = node.attr
                if module in PARTIAL_MODULES:
                    blocked_attrs = PARTIAL_MODULES[module].get("blocked_attrs", set())
                    if attr in blocked_attrs:
                        snippet = source_lines[node.lineno - 1].strip() if node.lineno <= len(source_lines) else None
                        findings.append(SecurityFinding(
                            severity=Severity.HIGH,
                            category="BLOCKED_ATTRIBUTE",
                            message=f"Blocked attribute access: {module}.{attr}",
                            line=node.lineno,
                            code_snippet=snippet,
                        ))

        return findings

    def _analyze_imports(self, code: str) -> List[SecurityFinding]:
        findings = []
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return findings

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name.split('.')[0]
                    if module in BLOCKED_MODULES:
                        findings.append(SecurityFinding(
                            severity=Severity.CRITICAL,
                            category="BLOCKED_IMPORT",
                            message=f"Blocked module import: {alias.name}",
                            line=node.lineno,
                        ))

            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    root_module = node.module.split('.')[0]
                    full_module = node.module
                    if root_module in BLOCKED_MODULES:
                        findings.append(SecurityFinding(
                            severity=Severity.CRITICAL,
                            category="BLOCKED_IMPORT",
                            message=f"Blocked module import: from {node.module}",
                            line=node.lineno,
                        ))
                    elif root_module in PARTIAL_MODULES:
                        allowed = PARTIAL_MODULES[root_module].get("allowed", set())
                        for alias in node.names:
                            full_path = f"{full_module}.{alias.name}"
                            if full_path not in allowed and full_module not in allowed:
                                if alias.name in PARTIAL_MODULES[root_module].get("blocked_attrs", set()):
                                    findings.append(SecurityFinding(
                                        severity=Severity.CRITICAL,
                                        category="BLOCKED_IMPORT",
                                        message=f"Blocked import: from {node.module} import {alias.name}",
                                        line=node.lineno,
                                    ))

        return findings

    def _analyze_patterns(self, code: str) -> List[SecurityFinding]:
        findings = []
        for regex, severity, message in self._compiled_patterns:
            for match in regex.finditer(code):
                line_num = code[:match.start()].count('\n') + 1
                findings.append(SecurityFinding(
                    severity=severity,
                    category="PATTERN_MATCH",
                    message=message,
                    line=line_num,
                    code_snippet=match.group(0)[:100],
                ))
        return findings

    def _get_call_name(self, node: ast.Call) -> str:
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            base = node.func.value
            if isinstance(base, ast.Name) and base.id in ("builtins", "__builtins__"):
                return node.func.attr
            return ""
        return ""
