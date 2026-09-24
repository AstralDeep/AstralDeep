"""Pure, stdlib-only timezone-aware evaluator for one_shot, interval, and cron schedule
kinds, stepping minute-by-minute to the next match; used by scheduler/store.py,
runner.py, api.py, and governance.py's interval floor.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Set

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

_INTERVAL_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

_CRON_SEARCH_MINUTES = 366 * 24 * 60


class ScheduleError(ValueError):
    pass


def _tz(name: str):
    if not name or name.upper() == "UTC" or ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception as exc:  # pragma: no cover
        raise ScheduleError(f"invalid timezone: {name}") from exc


def _parse_field(field: str, lo: int, hi: int) -> Set[int]:
    values: Set[int] = set()
    for part in field.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            base, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ScheduleError(f"invalid step in '{field}'")
        else:
            base = part
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, b = base.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(base)
        if start < lo or end > hi or start > end:
            raise ScheduleError(f"field value out of range in '{field}' ({lo}-{hi})")
        values.update(range(start, end + 1, step))
    return values


def parse_cron(expr: str):
    fields = expr.split()
    if len(fields) != 5:
        raise ScheduleError("cron expression must have exactly 5 fields: 'min hour dom mon dow'")
    minute = _parse_field(fields[0], 0, 59)
    hour = _parse_field(fields[1], 0, 23)
    dom = _parse_field(fields[2], 1, 31)
    month = _parse_field(fields[3], 1, 12)
    dow_raw = _parse_field(fields[4], 0, 7)
    dow = {0 if d == 7 else d for d in dow_raw}
    dom_restricted = fields[2] != "*"
    dow_restricted = fields[4] != "*"
    return minute, hour, dom, month, dow, dom_restricted, dow_restricted


def _cron_matches(dt: datetime, parsed) -> bool:
    minute, hour, dom, month, dow, dom_restricted, dow_restricted = parsed
    if dt.minute not in minute or dt.hour not in hour or dt.month not in month:
        return False
    py_dow = dt.weekday()
    cron_dow = (py_dow + 1) % 7
    day_ok_dom = dt.day in dom
    day_ok_dow = cron_dow in dow
    if dom_restricted and dow_restricted:
        return day_ok_dom or day_ok_dow
    if dom_restricted:
        return day_ok_dom
    if dow_restricted:
        return day_ok_dow
    return True


def _next_cron(expr: str, tzname: str, after: datetime) -> Optional[datetime]:
    parsed = parse_cron(expr)
    tz = _tz(tzname)
    local = after.astimezone(tz)
    candidate = (local + timedelta(minutes=1)).replace(second=0, microsecond=0)
    for _ in range(_CRON_SEARCH_MINUTES):
        if _cron_matches(candidate, parsed):
            return candidate.astimezone(timezone.utc)
        candidate += timedelta(minutes=1)
    return None  # pragma: no cover


def compute_next_run_ms(
    schedule_kind: str,
    schedule_expr: str,
    timezone_name: str,
    after_ms: int,
) -> Optional[int]:
    after = datetime.fromtimestamp(after_ms / 1000, tz=timezone.utc)

    if schedule_kind == "one_shot":
        raw = schedule_expr.strip().replace("Z", "+00:00")
        try:
            when = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ScheduleError(f"invalid one_shot timestamp: {schedule_expr}") from exc
        if when.tzinfo is None:
            when = when.replace(tzinfo=_tz(timezone_name))
        when = when.astimezone(timezone.utc)
        return int(when.timestamp() * 1000) if when > after else None

    if schedule_kind == "interval":
        m = _INTERVAL_RE.match(schedule_expr)
        if not m:
            raise ScheduleError(f"invalid interval: {schedule_expr} (use e.g. '15m', '2h', '1d')")
        n, unit = int(m.group(1)), m.group(2).lower()
        seconds = n * _UNIT_SECONDS[unit]
        if seconds <= 0:
            raise ScheduleError("interval must be positive")
        return after_ms + seconds * 1000

    if schedule_kind == "cron":
        nxt = _next_cron(schedule_expr, timezone_name, after)
        return int(nxt.timestamp() * 1000) if nxt else None

    raise ScheduleError(f"unknown schedule_kind: {schedule_kind}")


def interval_seconds(schedule_kind: str, schedule_expr: str) -> Optional[int]:
    if schedule_kind != "interval":
        return None
    m = _INTERVAL_RE.match(schedule_expr)
    if not m:
        raise ScheduleError(f"invalid interval: {schedule_expr}")
    return int(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()]
