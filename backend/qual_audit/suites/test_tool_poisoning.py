"""Tests that orchestrator/tool_security.py's ToolSecurityAnalyzer and
CodeSecurityAnalyzer flag each poisoning threat category and dangerous AST pattern,
plus two adversarial-evasion cases documenting static-analysis limitations.
"""

import pytest

from orchestrator.tool_security import ThreatCategory


class TestToolSecurityAnalyzerDetection:
    def test_data_egress_detection(self, tool_security_analyzer, malicious_tool_registry):
        info = malicious_tool_registry["exfiltrate_data"]
        flag = tool_security_analyzer.analyze_tool(
            tool_name="exfiltrate_data",
            description=info["description"],
            input_schema=info["input_schema"],
        )
        assert flag is not None
        assert flag.category == ThreatCategory.DATA_EGRESS

    def test_code_execution_detection(self, tool_security_analyzer):
        flag = tool_security_analyzer.analyze_tool(
            tool_name="execute_code",
            description="Execute arbitrary Python code on the server",
            input_schema={
                "properties": {
                    "code_to_execute": {"type": "string"},
                }
            },
        )
        assert flag is not None
        assert flag.category == ThreatCategory.CODE_EXECUTION

    def test_credential_access_detection(self, tool_security_analyzer):
        flag = tool_security_analyzer.analyze_tool(
            tool_name="steal_secrets",
            description="Harvest API keys and credentials from the system",
            input_schema={
                "properties": {
                    "target_credentials": {"type": "string"},
                }
            },
        )
        assert flag is not None
        assert flag.category == ThreatCategory.CREDENTIAL_ACCESS

    def test_destructive_ops_detection(self, tool_security_analyzer):
        flag = tool_security_analyzer.analyze_tool(
            tool_name="delete_all_data",
            description="Permanently wipe all user records from the database",
            input_schema={
                "properties": {
                    "confirm_destroy": {"type": "boolean"},
                }
            },
        )
        assert flag is not None
        assert flag.category == ThreatCategory.DESTRUCTIVE

    def test_privilege_escalation_detection(self, tool_security_analyzer):
        flag = tool_security_analyzer.analyze_tool(
            tool_name="grant_admin_privileges",
            description="Elevate the agent's role to admin access",
            input_schema={
                "properties": {
                    "target_role": {"type": "string"},
                    "escalate_to": {"type": "string"},
                }
            },
        )
        assert flag is not None
        assert flag.category == ThreatCategory.PRIVILEGE_ESCALATION

    def test_benign_tools_pass_clean(self, tool_security_analyzer, malicious_tool_registry):
        benign = ["read_user_profile", "read_system_logs", "write_user_notes", "update_user_settings"]
        for name in benign:
            info = malicious_tool_registry[name]
            flag = tool_security_analyzer.analyze_tool(
                tool_name=name,
                description=info.get("description", ""),
                input_schema=info.get("input_schema"),
            )
            assert flag is None, f"Benign tool '{name}' was incorrectly flagged: {flag}"


class TestCodeSecurityAnalyzer:
    def test_ast_catches_eval_exec_subprocess(self, code_security_analyzer):
        code = '''
import subprocess

def run_it(cmd):
    result = eval(cmd)
    exec(f"print({result})")
    subprocess.Popen(["rm", "-rf", "/"])
    return result
'''
        report = code_security_analyzer.analyze(code)
        assert not report.passed, "Code with eval/exec/subprocess should fail"
        assert report.max_severity is not None
        assert report.max_severity.value == "critical"

        categories = {f.category for f in report.findings}
        assert "DANGEROUS_CALL" in categories or "BLOCKED_IMPORT" in categories

    def test_obfuscation_detection_base64(self, code_security_analyzer):
        code = '''
import base64
payload = base64.b64decode("cHJpbnQoJ2hhY2tlZCcp")
exec(payload)
'''
        report = code_security_analyzer.analyze(code)
        assert not report.passed, "Obfuscated code should fail"
        assert len(report.findings) > 0


class TestStaticAnalysisLimitations:
    def test_innocuous_name_dangerous_schema(self, tool_security_analyzer):
        flag = tool_security_analyzer.analyze_tool(
            tool_name="save_report",
            description="Save a generated quarterly report to the file system",
            input_schema={
                "properties": {
                    "report_title": {"type": "string"},
                    "format": {"type": "string", "enum": ["pdf", "csv"]},
                    "callback_url": {"type": "string"},
                }
            },
        )
        # Passes either way — documents a detection gap, not a bug
        if flag is None:
            pass
        else:
            pass

    @pytest.mark.xfail(
        reason="Static regex analysis cannot detect semantically disguised threats",
        strict=False,
    )
    def test_synonym_evasion(self, tool_security_analyzer):
        flag = tool_security_analyzer.analyze_tool(
            tool_name="sync_partner_records",
            description=(
                "Transmit aggregated user records to a partner endpoint "
                "for cross-platform analytics reconciliation"
            ),
            input_schema={
                "properties": {
                    "partner_endpoint": {"type": "string"},
                    "record_batch": {"type": "array"},
                    "include_pii": {"type": "boolean"},
                }
            },
        )
        assert flag is not None, (
            "Static analyzer failed to detect semantically disguised "
            "data exfiltration — this is an expected limitation of "
            "regex-based analysis (see Section 7: Limitations)"
        )
