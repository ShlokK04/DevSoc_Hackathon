"""
Demo seed data so the app is explorable the moment it starts.

Creates a small world that exercises *both* matching paths:

  * A warm-intro cluster — Jamie, Alex, Sam, Dev with the sample timetables
    and the friend edges from the original demo story.
  * Three friendless students — Mei, Omar, Lina (e.g. just-arrived
    international students with no social graph). They can never appear in a
    warm triplet, so they exercise `run_open_matching`: grouped only with
    each other, still requiring a shared on-campus day.

Every account's password is `password` (printed on startup). Idempotent —
does nothing if users already exist.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .ics_parser import parse_ics_file
from .models import ClassEvent, Timetable
from .store import Store

DATA = Path(__file__).parent / "sample_data"

# zid -> (name, [course codes], ics file or None)
_WARM = {
    "z1": ("Jamie", ["COMP1531"], "jamie.ics"),
    "z2": ("Alex", ["COMP6080"], "alex.ics"),
    "z3": ("Sam", ["COMP2521"], "sam.ics"),
    "z4": ("Dev", ["COMP1521"], "dev.ics"),
}
_FRIENDSHIPS = [("z1", "z2"), ("z1", "z3"), ("z2", "z4")]

_FRIENDLESS = {
    "z5": ("Mei", ["COMP1511"]),
    "z6": ("Omar", ["COMP1511"]),
    "z7": ("Lina", ["COMP1511"]),
}


def _ev(h1: int, h2: int, where: str) -> ClassEvent:
    """A Wed 27 May 2026 on-campus class — the demo matching week."""
    d = (2026, 5, 27)
    return ClassEvent(
        "COMP1511 Class", where,
        datetime(*d, h1, 0), datetime(*d, h2, 0),
    )


# Staggered so all three are simultaneously free Wed 11:00–12:00 — exactly
# the gap the warm cluster also lands on, proving the same window maths runs
# for cold-start groups.
_FRIENDLESS_TT = {
    "z5": Timetable([_ev(9, 11, "Ainsworth Building G03"),
                     _ev(12, 14, "Ainsworth Building G03")]),
    "z6": Timetable([_ev(9, 10, "Ainsworth Building G03"),
                     _ev(14, 16, "Ainsworth Building G03")]),
    "z7": Timetable([_ev(10, 11, "K17 Building 113"),
                     _ev(16, 18, "K17 Building 113")]),
}


def seed(store: Store) -> bool:
    """Populate an empty store. Returns True if it actually seeded."""
    if store.all_users():
        return False

    for zid, (name, codes, ics) in _WARM.items():
        store.register(zid, name, "password")
        store.set_courses(zid, codes)
        store.set_timetable(zid, parse_ics_file(DATA / ics))

    for zid, (name, codes) in _FRIENDLESS.items():
        store.register(zid, name, "password")
        store.set_courses(zid, codes)
        store.set_timetable(zid, _FRIENDLESS_TT[zid])

    for a, b in _FRIENDSHIPS:
        store.seed_friendship(a, b)
    return True
