"""
A zero-dependency .ics parser — only what a UNSW timetable needs.

Handles the three things real exported timetables actually use:

  * **Line unfolding** (RFC 5545 §3.1): a CRLF followed by a space or tab is
    a continuation of the previous line.
  * **DTSTART/DTEND with a `TZID` parameter** (e.g.
    `DTSTART;TZID=Australia/Sydney:20260527T090000`), bare local times, and
    UTC (`...Z`). We keep wall-clock local time: the whole system reasons in
    campus-local time, so a naive datetime is the honest representation.
  * **Weekly `RRULE`** (`FREQ=WEEKLY` with `COUNT`/`UNTIL`, optional
    `INTERVAL` and `BYDAY`) — how a recurring class is encoded.

Anything it doesn't understand is skipped rather than guessed. The output is
a `Timetable` of concrete `ClassEvent` occurrences — the single source of
truth the matcher reads.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from .models import ClassEvent, Timetable

_BYDAY = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _unfold(text: str) -> list[str]:
    """Join RFC-5545 folded continuation lines back onto their owner."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _split_prop(line: str) -> tuple[str, dict[str, str], str]:
    """`DTSTART;TZID=Australia/Sydney:20260527T090000` -> name, params, value."""
    if ":" not in line:
        return line, {}, ""
    head, value = line.split(":", 1)
    name, *param_parts = head.split(";")
    params = {}
    for p in param_parts:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v
    return name.upper(), params, value.strip()


def _parse_dt(value: str) -> datetime | None:
    """Parse a DATE-TIME (or DATE) value into a naive local datetime."""
    v = value.strip().rstrip("Z")  # drop UTC marker; we keep wall-clock time
    try:
        if "T" in v:
            return datetime.strptime(v, "%Y%m%dT%H%M%S")
        return datetime.strptime(v, "%Y%m%d")
    except ValueError:
        return None


def _parse_rrule(value: str) -> dict[str, str]:
    return {
        k.upper(): v
        for part in value.split(";")
        if "=" in part
        for k, v in [part.split("=", 1)]
    }


def _expand(
    summary: str, location: str, start: datetime, end: datetime, rrule: str
) -> list[ClassEvent]:
    """One VEVENT -> its occurrences (a single event, or a weekly series)."""
    base = ClassEvent(summary, location, start, end)
    if not rrule:
        return [base]

    rule = _parse_rrule(rrule)
    if rule.get("FREQ") != "WEEKLY":
        return [base]  # only weekly recurrence is supported; emit the seed

    interval = int(rule.get("INTERVAL", "1") or "1")
    duration = end - start
    weekdays = [
        _BYDAY[d] for d in rule.get("BYDAY", "").split(",") if d in _BYDAY
    ] or [start.weekday()]

    # A bound is mandatory in practice; cap defensively so a malformed
    # unbounded rule can never spin forever.
    count = int(rule["COUNT"]) if "COUNT" in rule else None
    until = _parse_dt(rule["UNTIL"]) if "UNTIL" in rule else None
    hard_cap = 366

    events: list[ClassEvent] = []
    week_start = start - timedelta(days=start.weekday())
    week = 0
    while len(events) < hard_cap:
        for wd in sorted(weekdays):
            occ = (week_start + timedelta(weeks=week * interval, days=wd)).replace(
                hour=start.hour, minute=start.minute, second=start.second
            )
            if occ < start:
                continue
            if until and occ > until:
                return events
            events.append(ClassEvent(summary, location, occ, occ + duration))
            if count and len(events) >= count:
                return events
        if count is None and until is None:
            break  # no bound at all -> treat as a single occurrence
        week += 1
    return events


def parse_ics(text: str) -> Timetable:
    """Parse raw .ics text into a `Timetable`."""
    events: list[ClassEvent] = []
    in_event = False
    cur: dict[str, object] = {}

    for line in _unfold(text):
        name, _params, value = _split_prop(line)
        if name == "BEGIN" and value == "VEVENT":
            in_event, cur = True, {}
        elif name == "END" and value == "VEVENT":
            start, end = cur.get("DTSTART"), cur.get("DTEND")
            if isinstance(start, datetime) and isinstance(end, datetime):
                events.extend(
                    _expand(
                        str(cur.get("SUMMARY", "")),
                        str(cur.get("LOCATION", "")),
                        start,
                        end,
                        str(cur.get("RRULE", "")),
                    )
                )
            in_event = False
        elif in_event:
            if name in ("DTSTART", "DTEND"):
                cur[name] = _parse_dt(value)
            elif name in ("SUMMARY", "LOCATION", "RRULE"):
                cur[name] = value

    return Timetable(events)


def parse_ics_file(path: str | Path) -> Timetable:
    return parse_ics(Path(path).read_text(encoding="utf-8"))
