"""
Core data models for CampusThread.

These are deliberately small, immutable-ish value objects. The matching
algorithm only ever reads them, so there is no behaviour hidden in here
beyond a few derived helpers on the timetable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Iterable

# Weekday index -> short label (Python: Monday == 0)
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass(frozen=True)
class ClassEvent:
    """One concrete class occurrence parsed from a user's .ics file."""

    summary: str          # e.g. "COMP1531 Lecture"
    location: str         # e.g. "Ainsworth G03" or "" / "Online"
    start: datetime
    end: datetime

    @property
    def day(self) -> date:
        return self.start.date()

    @property
    def is_on_campus(self) -> bool:
        """A class is on campus if it has a physical, non-online location."""
        loc = self.location.strip().lower()
        if not loc:
            return False
        return not any(token in loc for token in ("online", "ed:online", "off campus", "remote"))


@dataclass
class Timetable:
    """A user's parsed timetable. The single source of truth for presence."""

    events: list[ClassEvent] = field(default_factory=list)

    def events_on(self, day: date) -> list[ClassEvent]:
        return sorted(
            (e for e in self.events if e.day == day),
            key=lambda e: e.start,
        )

    def on_campus_days(self, week_start: date) -> set[date]:
        """Mon–Fri days in the given week where the user has >=1 on-campus class."""
        days: set[date] = set()
        for e in self.events:
            offset = (e.day - week_start).days
            if 0 <= offset <= 4 and e.is_on_campus:   # Mon..Fri only
                days.add(e.day)
        return days


@dataclass
class Course:
    """Enrichment record sourced from the UNSW Courses API."""

    course_code: str
    course_name: str
    faculty: str
    school: str


@dataclass
class User:
    zid: str
    name: str
    course_codes: list[str]
    timetable: Timetable
    not_on_campus_this_week: bool = False   # manual opt-out toggle

    def __hash__(self) -> int:
        return hash(self.zid)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, User) and other.zid == self.zid


@dataclass
class MeetupWindow:
    """A time slot where all three members are simultaneously free on campus."""

    day: date
    start: time
    end: time

    @property
    def minutes(self) -> int:
        s = self.start.hour * 60 + self.start.minute
        e = self.end.hour * 60 + self.end.minute
        return e - s

    def label(self) -> str:
        return (
            f"{WEEKDAYS[self.day.weekday()]} "
            f"{self.start.strftime('%H:%M')}–{self.end.strftime('%H:%M')}"
        )


@dataclass
class RoomSuggestion:
    """A bookable space free during a meetup window, near the trio."""

    building_id: str
    building_name: str
    room_id: str
    room_name: str
    capacity: int
    usage: str
    distance_m: float | None  # walking-proxy distance from where they already are

    def label(self) -> str:
        near = f" · ~{round(self.distance_m)}m away" if self.distance_m else ""
        return f"{self.room_name} ({self.building_name}, seats {self.capacity}){near}"


@dataclass
class Match:
    """The output of the matching algorithm: a created group chat."""

    connector: User                 # User A — friends with both others
    member_b: User
    member_c: User
    shared_campus_days: list[date]
    shared_faculty: str | None      # set if >=2 members share a faculty
    meetup_windows: list[MeetupWindow]
    score: tuple                    # ranking key, kept for transparency
    # Filled in later by the room resolver (needs network); aligned 1:1 with
    # meetup_windows. The matcher never sets this — it stays network-free.
    room_suggestions: list[RoomSuggestion | None] = field(default_factory=list)
    # "warm-intro" = via a mutual friend (connector). "open" = a cold-start
    # group of friendless / missed-the-window users (no connector). Additive
    # with a default, so existing matches and tests are unaffected.
    kind: str = "warm-intro"

    def members(self) -> tuple[User, User, User]:
        return (self.connector, self.member_b, self.member_c)

    def member_zids(self) -> set[str]:
        return {self.connector.zid, self.member_b.zid, self.member_c.zid}
