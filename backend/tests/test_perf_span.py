"""Tests for backend/shared/perf.py's perf_span timing helper: structured log line
format, duration measurement, logging on exception, and omission of trailing context
when none is given.
"""

import logging
import re

import pytest

from shared.perf import perf_span


def test_perf_span_emits_structured_line(caplog):
    with caplog.at_level(logging.INFO, logger="astral.perf"):
        with perf_span("surface.render.agents", surface="agents", user="u1"):
            pass
    line = caplog.records[-1].getMessage()
    assert re.fullmatch(
        r"perf surface\.render\.agents duration_ms=\d+ surface=agents user=u1", line
    )


def test_perf_span_duration_reflects_elapsed_time(caplog):
    with caplog.at_level(logging.INFO, logger="astral.perf"):
        with perf_span("t"):
            pass
    match = re.search(r"duration_ms=(\d+)", caplog.records[-1].getMessage())
    assert match is not None
    assert 0 <= int(match.group(1)) < 10_000


def test_perf_span_logs_even_when_block_raises(caplog):
    with caplog.at_level(logging.INFO, logger="astral.perf"):
        with pytest.raises(ValueError):
            with perf_span("boom", chat="c9"):
                raise ValueError("x")
    line = caplog.records[-1].getMessage()
    assert line.startswith("perf boom duration_ms=")
    assert line.endswith(" chat=c9")


def test_perf_span_without_context_has_no_trailing_space(caplog):
    with caplog.at_level(logging.INFO, logger="astral.perf"):
        with perf_span("bare"):
            pass
    assert re.fullmatch(r"perf bare duration_ms=\d+", caplog.records[-1].getMessage())
