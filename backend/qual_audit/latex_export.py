"""Generates the qualification report's LaTeX tables and appendices (per-category,
frontend, summary, CLEAR-framework, audit log, and benchmark charts) from verified
qual_audit/database.py results, driven by qual_audit/cli.py's export command.
"""

import json
import os
from typing import Dict, List

from jinja2 import Environment, FileSystemLoader

from qual_audit.database import AuditDatabase
from qual_audit.models import LatexArtifact, TestCaseResult, VerificationStatus

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")

_DEFAULT_OUTPUT = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__), "..", "..",
        "Qualifying_Exam", "sources", "tables",
    )
)


def _latex_escape(text: str) -> str:
    replacements = {
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    for char, replacement in replacements.items():
        text = text.replace(char, replacement)
    return text


def _get_jinja_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        block_start_string="<%",
        block_end_string="%>",
        variable_start_string="<<",
        variable_end_string=">>",
        comment_start_string="<#",
        comment_end_string="#>",
    )
    env.filters["latex_escape"] = _latex_escape
    return env


_CATEGORY_LABELS = {
    "tool_poisoning": "Tool Poisoning",
    "prompt_injection": "Prompt Injection",
    "rote_adaptation": "ROTE Device Adaptation",
    "permission_delegation": "Permission \\& Delegation",
    "transport_comparison": "Transport Comparison",
    "frontend_rendering": "Frontend Rendering",
    "cost_overhead": "Security Cost Overhead",
    "parallel_dispatch": "Parallel Multi-Agent Dispatch",
}

_CATEGORY_PREFIX = {
    "parallel_dispatch": "PD-B",
}

_CLEAR_MAPPING = {
    "cost_overhead": "Cost",
    "transport_comparison": "Latency / Reliability",
    "frontend_rendering": "Efficacy",
    "rote_adaptation": "Efficacy",
    "tool_poisoning": "Assurance",
    "prompt_injection": "Assurance",
    "permission_delegation": "Assurance",
    "parallel_dispatch": "Latency / Reliability",
}

_CLEAR_DESCRIPTIONS = {
    "Cost": "Security processing overhead and computational expenditure",
    "Latency / Reliability": "Transport response time, throughput, ordering, and parallel dispatch performance",
    "Efficacy": "UI rendering fidelity and device adaptation correctness",
    "Assurance": "Authorization enforcement, threat detection, and injection resistance",
}

_TEST_DESCRIPTIONS: Dict[str, str] = {
    "test_ast_catches_eval_exec_subprocess": "AST catches eval, exec, subprocess calls",
    "test_obfuscation_detection_base64": "Detects base64-obfuscated execution",
    "test_benign_tools_pass_clean": "Benign tools produce no false positives",
    "test_code_execution_detection": "Flags arbitrary code execution tools",
    "test_credential_access_detection": "Flags credential harvesting tools",
    "test_data_egress_detection": "Flags data exfiltration tools",
    "test_destructive_ops_detection": "Flags destructive operation tools",
    "test_privilege_escalation_detection": "Flags privilege escalation tools",
    "test_class_hierarchy_traversal_blocked": "Blocks Python class hierarchy sandbox escape",
    "test_encoded_injection_detection": "Detects base64-encoded injection payload",
    "test_pickle_deserialization_blocked": "Blocks unsafe pickle deserialization",
    "test_system_prompt_override_in_code": "Flags eval-based prompt override in code",
    "test_tool_response_injection_blocked": "Blocks exec injection via tool responses",
    "test_browser_passthrough": "Browser passes all components unchanged",
    "test_button_tv_removed": "TV profile removes interactive buttons",
    "test_chart_watch_degradation": "Watch degrades bar chart to metric card",
    "test_code_block_mobile_removed": "Mobile removes unsupported code blocks",
    "test_grid_collapse_mobile": "Mobile collapses 4-col grid to 1-col",
    "test_table_mobile_truncation": "Mobile truncates table to 4 cols, 20 rows",
    "test_table_tablet_column_limit": "Tablet limits table to 6 columns",
    "test_voice_text_extraction": "Voice extracts text-only, max 300 chars",
    "test_cross_user_isolation": "Different users have independent permissions",
    "test_per_tool_override": "Per-tool override disables specific tool",
    "test_permission_change_immediate_effect": "Scope toggle takes effect immediately",
    "test_scope_enforcement_blocks_unauthorized": "Read-only scope blocks write tool access",
    "test_token_act_claim_structure": "Token act claim follows RFC 8693 format",
    "test_token_attenuation_scopes": "Delegation token contains only granted scopes",
    "test_sse_echo_latency": "SSE round-trip echo latency (N=100)",
    "test_sse_throughput": "SSE echo throughput in messages/sec",
    "test_sse_message_ordering": "SSE messages arrive in send order",
    "test_sse_reconnection_id": "SSE connection identity for reconnection",
    "test_sse_concurrent_connections": "SSE 10 concurrent connections fairness",
    "test_ws_echo_latency": "WS round-trip echo latency (N=100)",
    "test_ws_throughput": "WS echo throughput in messages/sec",
    "test_ws_message_ordering": "WS messages arrive in send order",
    "test_ws_reconnection": "WS reconnection with session continuity",
    "test_ws_concurrent_connections": "WS 10 concurrent connections fairness",
    "test_tool_analyzer_timing": "ToolSecurityAnalyzer mean/p95 latency (10-tool corpus)",
    "test_code_analyzer_timing": "CodeSecurityAnalyzer timing across code complexities",
    "test_combined_registration_overhead": "Combined security screening overhead per registration",
    "test_memory_overhead": "Memory delta during 50-tool batch analysis",
    "test_innocuous_name_dangerous_schema": "Innocuous name with subtly dangerous schema field",
    "test_synonym_evasion": "Synonym/paraphrasing evasion of regex patterns (xfail)",
    "test_sequential_vs_parallel_latency": "Sequential vs parallel latency (3 real agents)",
    "test_parallel_dispatch_correctness": "All parallel results returned without errors",
    "test_parallel_speedup_factor": "Parallel speedup factor over multiple trials",
}

_FRONTEND_GROUPS = {
    "Component Graceful Degradation": "Graceful Degradation",
    "SDUI Component Rendering via DynamicRenderer": "SDUI Component Rendering via DynamicRenderer",
}


def _short_name(test_name: str) -> str:
    parts = test_name.split("::")
    return parts[-1] if parts else test_name


def _abbreviated_name(func_name: str) -> str:
    name = func_name
    if name.startswith("test_"):
        name = name[5:]
    parts = name.split("_")
    if len(parts) > 3:
        name = "_".join(parts[:3])
    return name


def _get_description(test_name: str) -> str:
    func = _short_name(test_name)
    if func in _TEST_DESCRIPTIONS:
        return _TEST_DESCRIPTIONS[func]
    if ">" in test_name and ":" in test_name:
        after_colon = test_name.split(":", 1)[-1].strip()
        return after_colon[0].upper() + after_colon[1:] if after_colon else test_name
    return func


def _parse_frontend_short_name(test_name: str) -> str:
    if ">" in test_name and ":" in test_name:
        after_gt = test_name.split(">", 1)[-1].strip()
        sub_id = after_gt.split(":", 1)[0].strip()
        return sub_id
    return _abbreviated_name(_short_name(test_name))


def _parse_frontend_group(test_name: str) -> str:
    if ">" in test_name:
        return test_name.split(">", 1)[0].strip()
    return "Other"


def generate_category_table(
    cases: List[TestCaseResult], category: str, output_dir: str
) -> str:
    env = _get_jinja_env()
    template = env.get_template("category_section.tex.j2")

    rows = []
    for i, case in enumerate(cases, 1):
        prefix = _CATEGORY_PREFIX.get(category, category[:2].upper())
        func = _short_name(case.test_name)
        id_num = f"{i:02d}" if category in _CATEGORY_PREFIX else f"{i:03d}"
        rows.append({
            "id": f"{prefix}{id_num}" if category in _CATEGORY_PREFIX else f"{prefix}-{id_num}",
            "description": _latex_escape(_get_description(case.test_name)),
            "short_name": _latex_escape(_abbreviated_name(func)),
            "outcome": "Pass" if case.outcome.value == "passed" else "Fail",
            "outcome_mark": r"\checkmark" if case.outcome.value == "passed" else r"$\times$",
            "duration_ms": f"{case.duration_ms:.1f}",
        })

    label = _CATEGORY_LABELS.get(category, _latex_escape(category))
    content = template.render(
        category_label=label,
        category_id=category,
        rows=rows,
    )

    filename = f"{category}_table.tex"
    filepath = os.path.join(output_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    return filename


def generate_frontend_table(
    cases: List[TestCaseResult], output_dir: str
) -> str:
    env = _get_jinja_env()
    template = env.get_template("frontend_section.tex.j2")

    grouped: Dict[str, List] = {}
    for case in cases:
        group_key = _parse_frontend_group(case.test_name)
        grouped.setdefault(group_key, []).append(case)

    groups = []
    counter = 1
    for group_key, group_cases in grouped.items():
        label = _FRONTEND_GROUPS.get(group_key, _latex_escape(group_key))
        rows = []
        for case in group_cases:
            rows.append({
                "id": f"FR-{counter:03d}",
                "description": _latex_escape(_get_description(case.test_name)),
                "short_name": _latex_escape(_parse_frontend_short_name(case.test_name)),
                "outcome": "Pass" if case.outcome.value == "passed" else "Fail",
                "outcome_mark": r"\checkmark" if case.outcome.value == "passed" else r"$\times$",
                "duration_ms": f"{case.duration_ms:.1f}",
            })
            counter += 1
        groups.append({"label": label, "rows": rows})

    content = template.render(groups=groups)

    filename = "frontend_rendering_table.tex"
    filepath = os.path.join(output_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    return filename


def generate_summary_table(
    all_cases: List[TestCaseResult], output_dir: str
) -> str:
    env = _get_jinja_env()
    template = env.get_template("summary_table.tex.j2")

    suites: Dict[str, List[TestCaseResult]] = {}
    for c in all_cases:
        suites.setdefault(c.suite, []).append(c)

    rows = []
    for suite, cases in sorted(suites.items()):
        total = len(cases)
        passed = sum(1 for c in cases if c.outcome.value == "passed")
        failed = total - passed
        rows.append({
            "category": _CATEGORY_LABELS.get(suite, _latex_escape(suite)),
            "total": total,
            "passed": passed,
            "failed": failed,
        })

    content = template.render(rows=rows)
    filename = "summary_table.tex"
    with open(os.path.join(output_dir, filename), "w", encoding="utf-8") as f:
        f.write(content)
    return filename


def generate_clear_summary_table(
    all_cases: List[TestCaseResult], output_dir: str
) -> str:
    env = _get_jinja_env()
    template = env.get_template("clear_summary.tex.j2")

    dimensions: Dict[str, List[TestCaseResult]] = {}
    for c in all_cases:
        dim = _CLEAR_MAPPING.get(c.suite, "Other")
        dimensions.setdefault(dim, []).append(c)

    dim_order = ["Cost", "Latency / Reliability", "Efficacy", "Assurance"]
    rows = []
    for dim in dim_order:
        cases = dimensions.get(dim, [])
        if not cases:
            continue
        total = len(cases)
        passed = sum(1 for c in cases if c.outcome.value == "passed")
        suites = sorted({c.suite for c in cases})
        suite_labels = ", ".join(
            _CATEGORY_LABELS.get(s, s) for s in suites
        )
        rows.append({
            "dimension": dim,
            "description": _CLEAR_DESCRIPTIONS.get(dim, ""),
            "suites": suite_labels,
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": f"{(passed / total * 100):.0f}\\%" if total > 0 else "---",
        })

    content = template.render(rows=rows)
    filename = "clear_mapping_table.tex"
    with open(os.path.join(output_dir, filename), "w", encoding="utf-8") as f:
        f.write(content)
    return filename


def generate_audit_appendix(db: AuditDatabase, run_id: str, output_dir: str) -> str:
    env = _get_jinja_env()
    template = env.get_template("audit_appendix.tex.j2")

    cases = db.get_cases_for_run(run_id)

    cases.sort(key=lambda c: (c.suite, c.test_name))

    rows = []
    suite_counters: Dict[str, int] = {}
    for case in cases:
        suite = case.suite
        suite_counters[suite] = suite_counters.get(suite, 0) + 1
        prefix = suite[:2].upper()
        rows.append({
            "id": f"{prefix}-{suite_counters[suite]:03d}",
            "case_name": _latex_escape(_get_description(case.test_name)),
            "outcome": "Pass" if case.outcome.value == "passed" else "Fail",
            "outcome_mark": r"\checkmark" if case.outcome.value == "passed" else r"$\times$",
            "duration_ms": f"{case.duration_ms:.1f}",
        })

    content = template.render(rows=rows)
    filename = "audit_appendix.tex"
    with open(os.path.join(output_dir, filename), "w", encoding="utf-8") as f:
        f.write(content)
    return filename


def _split_transport_metrics(cases: List[TestCaseResult]) -> Dict[str, float]:
    metrics: Dict[str, float] = {}

    for c in cases:
        if c.metrics:
            metrics.update(c.metrics)

    if not metrics:
        import statistics

        sse_cases = [c for c in cases if "sse" in _short_name(c.test_name)]
        ws_cases = [c for c in cases if "ws" in _short_name(c.test_name)]

        for prefix, subset in [("sse", sse_cases), ("ws", ws_cases)]:
            durations = sorted([c.duration_ms for c in subset if c.duration_ms > 0])
            if durations:
                n = len(durations)
                metrics[f"{prefix}_mean_ms"] = round(statistics.mean(durations), 2)
                metrics[f"{prefix}_median_ms"] = round(statistics.median(durations), 2)
                metrics[f"{prefix}_p95_ms"] = round(
                    durations[int(n * 0.95)] if n > 1 else durations[-1], 2
                )
                metrics[f"{prefix}_p99_ms"] = round(durations[-1], 2)

    return metrics


def generate_benchmark_chart(
    cases: List[TestCaseResult], output_dir: str
) -> str:
    env = _get_jinja_env()
    template = env.get_template("benchmark_chart.tex.j2")

    metrics_data = _split_transport_metrics(cases)
    content = template.render(metrics=metrics_data)
    filename = "transport_latency_chart.tex"
    with open(os.path.join(output_dir, filename), "w", encoding="utf-8") as f:
        f.write(content)
    return filename


def print_transport_summary(cases: List[TestCaseResult]) -> None:
    metrics = _split_transport_metrics(cases)

    sse_tp_cases = [c for c in cases if "throughput" in _short_name(c.test_name) and "sse" in _short_name(c.test_name)]
    ws_tp_cases = [c for c in cases if "throughput" in _short_name(c.test_name) and "ws" in _short_name(c.test_name)]

    sse_throughput = "---"
    ws_throughput = "---"
    if sse_tp_cases and sse_tp_cases[0].duration_ms > 0:
        sse_throughput = f"{200 / (sse_tp_cases[0].duration_ms / 1000):.0f}"
    if ws_tp_cases and ws_tp_cases[0].duration_ms > 0:
        ws_throughput = f"{200 / (ws_tp_cases[0].duration_ms / 1000):.0f}"

    print("\n+----------------------------------------------------------------------+")
    print("|              Transport Performance Summary                          |")
    print("+----------------------------------------------------------------------+")
    print(f"|  SSE (HTTP): {sse_throughput:>6} msg/s  |  "
          f"Mean: {metrics.get('sse_mean_ms', '---'):>8}ms  "
          f"Median: {metrics.get('sse_median_ms', '---'):>8}ms  "
          f"P95: {metrics.get('sse_p95_ms', '---'):>8}ms  "
          f"P99: {metrics.get('sse_p99_ms', '---'):>8}ms")
    print(f"|  WebSocket:  {ws_throughput:>6} msg/s  |  "
          f"Mean: {metrics.get('ws_mean_ms', '---'):>8}ms  "
          f"Median: {metrics.get('ws_median_ms', '---'):>8}ms  "
          f"P95: {metrics.get('ws_p95_ms', '---'):>8}ms  "
          f"P99: {metrics.get('ws_p99_ms', '---'):>8}ms")
    print("+----------------------------------------------------------------------+\n")


def print_parallel_dispatch_summary():
    import tempfile
    bench_file = os.path.join(tempfile.gettempdir(), "astral_parallel_dispatch_bench.json")
    if not os.path.exists(bench_file):
        return

    with open(bench_file, "r") as f:
        data = json.load(f)

    lat = data.get("latency", {})
    spd = data.get("speedup_trials", {})

    print("+----------------------------------------------------------------------+")
    print("|           Parallel Multi-Agent Dispatch Summary                      |")
    print("+----------------------------------------------------------------------+")
    if lat:
        print(f"|  Agents dispatched: {lat.get('agent_count', '?')}")
        print("|    Weather  -- get_current_weather(city='New York')")
        print("|    General  -- get_system_status()")
        print("|    Medical  -- search_patients(30-60, diabetes)")
        print(f"|  Sequential: {lat.get('sequential_ms', '---')}ms")
        print(f"|  Parallel:   {lat.get('parallel_ms', '---')}ms")
        print(f"|  Speedup:    {lat.get('speedup', '---')}x")
    if spd:
        print(f"|  Avg speedup ({spd.get('n_trials', '?')} warm trials): {spd.get('avg_speedup', '---')}x")
        individual = spd.get("individual", [])
        if individual:
            trial_str = ", ".join(f"{s}x" for s in individual)
            print(f"|  Individual trials: {trial_str}")
    print("+----------------------------------------------------------------------+\n")

    os.unlink(bench_file)


def generate_all_artifacts(
    db: AuditDatabase, run_id: str, output_dir: str
) -> List[LatexArtifact]:
    cases = db.get_cases_for_run(run_id)
    artifacts: List[LatexArtifact] = []

    suites: Dict[str, List[TestCaseResult]] = {}
    for c in cases:
        suites.setdefault(c.suite, []).append(c)

    for suite, suite_cases in sorted(suites.items()):
        if suite == "frontend_rendering":
            filename = generate_frontend_table(suite_cases, output_dir)
        else:
            filename = generate_category_table(suite_cases, suite, output_dir)
        art = LatexArtifact(
            run_id=run_id,
            filename=filename,
            generated_from=[c.id for c in suite_cases],
            verification_complete=all(
                c.verification_status == VerificationStatus.VERIFIED for c in suite_cases
            ),
        )
        db.insert_artifact(art)
        artifacts.append(art)

    if "transport_comparison" in suites:
        tc_cases = suites["transport_comparison"]
        filename = generate_benchmark_chart(tc_cases, output_dir)
        art = LatexArtifact(
            run_id=run_id,
            filename=filename,
            generated_from=[c.id for c in tc_cases],
            verification_complete=True,
        )
        db.insert_artifact(art)
        artifacts.append(art)

        print_transport_summary(tc_cases)

    if "parallel_dispatch" in suites:
        print_parallel_dispatch_summary()

    filename = generate_clear_summary_table(cases, output_dir)
    art = LatexArtifact(
        run_id=run_id,
        filename=filename,
        generated_from=[c.id for c in cases],
        verification_complete=True,
    )
    db.insert_artifact(art)
    artifacts.append(art)

    filename = generate_summary_table(cases, output_dir)
    art = LatexArtifact(
        run_id=run_id,
        filename=filename,
        generated_from=[c.id for c in cases],
        verification_complete=True,
    )
    db.insert_artifact(art)
    artifacts.append(art)

    filename = generate_audit_appendix(db, run_id, output_dir)
    art = LatexArtifact(
        run_id=run_id,
        filename=filename,
        generated_from=[],
        verification_complete=True,
    )
    db.insert_artifact(art)
    artifacts.append(art)

    return artifacts
