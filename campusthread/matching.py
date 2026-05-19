"""
The CampusThread matching algorithm.

Run every Sunday 7pm. Each step below maps directly to the brief.

  1. Build candidate triplets from the social graph (connector A knows
     B and C; B and C don't know each other).
  2. For each triplet, parse all three timetables and find the days that
     week where ALL THREE are on campus.
  3. HARD CONSTRAINT: discard any triplet with zero shared on-campus days.
     This is never bypassed.
  4. Rank surviving triplets:
        primary   : number of shared on-campus days   (more is better)
        secondary : faculty cohesion                   (size of the largest
                    same-faculty cluster among the three; shared faculty =
                    stronger icebreaker)
        tertiary  : total simultaneous free minutes on shared days
                    (a sensible extra tiebreak — a match they can actually
                    act on beats one they can't)
  5. Greedily create chats from the ranked list, ensuring every user is in
     at most one chat that week. If a user has no valid triplet, they are
     skipped — never force a bad match.

The free-period maths in here also produces the 2–3 suggested meetup
windows the groupchat UI and icebreaker need, so it lives with matching
rather than being recomputed later.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta

from typing import TYPE_CHECKING

from .models import Match, MeetupWindow, User
from .social_graph import SocialGraph
from .unsw_courses import UnswCoursesClient

if TYPE_CHECKING:  # avoid a runtime import cycle; annotation only
    from .feedback import FeedbackStore

# Campus window the meetup search is bounded to, and the shortest gap worth
# suggesting. Tunable knobs, deliberately not magic numbers in the logic.
CAMPUS_DAY_START = time(9, 0)
CAMPUS_DAY_END = time(18, 0)
MIN_MEETUP_MINUTES = 30
MAX_MEETUP_WINDOWS = 3


# --------------------------------------------------------------------------- #
# Interval helpers (minutes-from-midnight on a single day)
# --------------------------------------------------------------------------- #

def _to_min(t: time) -> int:
    return t.hour * 60 + t.minute


def _from_min(m: int) -> time:
    return time(m // 60, m % 60)


def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sort and merge overlapping/touching [start, end) intervals."""
    if not intervals:
        return []
    intervals = sorted(intervals)
    merged = [intervals[0]]
    for s, e in intervals[1:]:
        ls, le = merged[-1]
        if s <= le:
            merged[-1] = (ls, max(le, e))
        else:
            merged.append((s, e))
    return merged


def _free_gaps(busy: list[tuple[int, int]], lo: int, hi: int) -> list[tuple[int, int]]:
    """Complement of `busy` within [lo, hi)."""
    gaps: list[tuple[int, int]] = []
    cursor = lo
    for s, e in _merge(busy):
        if s > cursor:
            gaps.append((cursor, min(s, hi)))
        cursor = max(cursor, e)
        if cursor >= hi:
            break
    if cursor < hi:
        gaps.append((cursor, hi))
    return [(s, e) for s, e in gaps if e > s]


# --------------------------------------------------------------------------- #
# Per-triplet evaluation
# --------------------------------------------------------------------------- #

def _shared_campus_days(members: tuple[User, User, User], week_start: date) -> list[date]:
    day_sets = [m.timetable.on_campus_days(week_start) for m in members]
    return sorted(set.intersection(*day_sets)) if all(day_sets) else []


def _faculty_cohesion(
    members: tuple[User, User, User], courses: UnswCoursesClient
) -> tuple[int, str | None]:
    """
    Return (size_of_largest_faculty_cluster, that_faculty).

    A user contributes every faculty across their enrolled courses. The
    cluster size is how many of the three members touch the most common
    faculty (2 or 3 means there's a shared icebreaker).
    """
    counter: Counter[str] = Counter()
    for m in members:
        faculties = set()
        for code in m.course_codes:
            course = courses.get_course(code)
            if course:
                faculties.add(course.faculty)
        counter.update(faculties)
    if not counter:
        return (1, None)
    faculty, cluster = counter.most_common(1)[0]
    return (cluster, faculty if cluster >= 2 else None)


def _meetup_windows(
    members: tuple[User, User, User], shared_days: list[date]
) -> list[MeetupWindow]:
    """Slots on shared days where all three are simultaneously free on campus."""
    windows: list[MeetupWindow] = []
    lo, hi = _to_min(CAMPUS_DAY_START), _to_min(CAMPUS_DAY_END)

    for day in shared_days:
        busy: list[tuple[int, int]] = []
        for m in members:
            for ev in m.timetable.events_on(day):
                busy.append((_to_min(ev.start.time()), _to_min(ev.end.time())))
        for s, e in _free_gaps(busy, lo, hi):
            if e - s >= MIN_MEETUP_MINUTES:
                windows.append(MeetupWindow(day, _from_min(s), _from_min(e)))

    # Longest first, then earliest — the most usable suggestions surface.
    windows.sort(key=lambda w: (-w.minutes, w.day, _to_min(w.start)))
    return windows[:MAX_MEETUP_WINDOWS]


# --------------------------------------------------------------------------- #
# The algorithm
# --------------------------------------------------------------------------- #

def run_weekly_matching(
    users: dict[str, User],
    graph: SocialGraph,
    courses: UnswCoursesClient,
    week_start: date,
    feedback: "FeedbackStore | None" = None,
    cooldown_weeks: int = 3,
) -> list[Match]:
    """
    Produce this week's group chats. `week_start` is the Monday Mon–Fri.

    `feedback` is optional and additive: when omitted, behaviour is exactly
    as before. When supplied, the We-met loop is honoured — met pairs and
    recently-rested pairs are filtered out, and proven matchmakers get a
    small, bounded ranking nudge (never above the day/faculty ordering).
    """

    blocked: set = set()
    if feedback is not None:
        blocked |= feedback.met_pairs()
        blocked |= feedback.rested_pairs(week_start, cooldown_weeks)

    scored: list[Match] = []

    for connector_id, b_id, c_id in graph.candidate_triplets():
        ids = (connector_id, b_id, c_id)
        if any(i not in users for i in ids):
            continue

        # We-met feedback: don't re-thread a pair that already met, nor one
        # rested during its cooldown after an introduction that didn't take.
        if feedback is not None and frozenset((b_id, c_id)) in blocked:
            continue

        members = (users[connector_id], users[b_id], users[c_id])

        # Manual opt-out — respected before any timetable work.
        if any(m.not_on_campus_this_week for m in members):
            continue

        shared_days = _shared_campus_days(members, week_start)
        if not shared_days:
            continue  # HARD CONSTRAINT — never bypassed.

        cluster, faculty = _faculty_cohesion(members, courses)
        windows = _meetup_windows(members, shared_days)
        total_free = sum(w.minutes for w in windows)

        # Proven-matchmaker nudge from the We-met loop. Bounded and placed
        # AFTER the brief's primary (days) and secondary (faculty) keys, so
        # it only ever breaks ties — it can never override a better match.
        connector_quality = (
            min(feedback.connector_success(connector_id), 3)
            if feedback is not None
            else 0
        )

        score = (len(shared_days), cluster, connector_quality, total_free)
        scored.append(
            Match(
                connector=members[0],
                member_b=members[1],
                member_c=members[2],
                shared_campus_days=shared_days,
                shared_faculty=faculty,
                meetup_windows=windows,
                score=score,
            )
        )

    # Rank: best score first.
    scored.sort(key=lambda m: m.score, reverse=True)

    # Greedy assignment — one chat per user per week.
    used: set[str] = set()
    matches: list[Match] = []
    for m in scored:
        if m.member_zids() & used:
            continue
        matches.append(m)
        used |= m.member_zids()

    return matches


def next_monday(today: date | None = None) -> date:
    """The Monday of the coming week (the Sunday-cron target window)."""
    today = today or date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)
