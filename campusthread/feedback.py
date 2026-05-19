"""
The "We met" feedback loop.

This closes CampusThread's core loop. When a thread's members tap "We met"
(and optionally add each other), three things must feed back into future
matching — exactly as the brief describes:

  1. **Friend-add mutates the graph.** A new B–C edge means the pair is no
     longer a warm introduction (they now know each other), and it grows
     everyone's second-degree reach — so *new* introductions become possible.
  2. **A met pair is not re-introduced.** The product already did its job for
     them; re-threading them is noise.
  3. **A pair introduced but NOT met is rested.** Absence of "we met" is
     itself a signal. Don't re-suggest the same non-connection every week —
     a cooldown lets the network try other paths first.

Plus a bounded positive signal: a connector whose past introductions led to
real meetups is a proven matchmaker, so their future triplets get a small
ranking nudge (always below the brief's primary day / faculty ordering).

The store persists to JSON so the loop survives across Sunday runs (the
demo's stand-in for the PostgreSQL the brief specifies). All of this is
local — `matching.py` stays network-free.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .models import Match
from .social_graph import SocialGraph

Pair = frozenset  # an unordered {zid, zid}


def _pair(a: str, b: str) -> frozenset:
    return frozenset((a, b))


@dataclass
class Introduction:
    """One thread that was created, and what came of it."""

    connector: str
    b: str
    c: str
    week: date
    met: bool = False
    friended: list[list[str]] = field(default_factory=list)  # new edges to apply

    @property
    def pair(self) -> frozenset:
        return _pair(self.b, self.c)

    def to_json(self) -> dict:
        return {
            "connector": self.connector,
            "b": self.b,
            "c": self.c,
            "week": self.week.isoformat(),
            "met": self.met,
            "friended": self.friended,
        }

    @staticmethod
    def from_json(d: dict) -> "Introduction":
        return Introduction(
            connector=d["connector"],
            b=d["b"],
            c=d["c"],
            week=date.fromisoformat(d["week"]),
            met=d.get("met", False),
            friended=[list(p) for p in d.get("friended", [])],
        )


class FeedbackStore:
    """Append-only log of introductions and their outcomes, JSON-backed."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.intros: list[Introduction] = []
        if self.path and self.path.exists():
            self.load()

    # -- persistence ------------------------------------------------------- #

    def load(self) -> None:
        data = json.loads(self.path.read_text())
        self.intros = [Introduction.from_json(d) for d in data]

    def save(self) -> None:
        if self.path:
            self.path.write_text(
                json.dumps([i.to_json() for i in self.intros], indent=2)
            )

    # -- recording --------------------------------------------------------- #

    def record_introductions(self, matches: list[Match], week: date) -> None:
        """Log every thread created this Sunday as a pending introduction."""
        for m in matches:
            self.intros.append(
                Introduction(
                    connector=m.connector.zid,
                    b=m.member_b.zid,
                    c=m.member_c.zid,
                    week=week,
                )
            )
        self.save()

    def record_we_met(
        self,
        b: str,
        c: str,
        week: date,
        *,
        friended: bool = False,
    ) -> None:
        """Mark the (b, c) introduction as a real meetup; optionally friends."""
        intro = self._find(b, c, week) or self._latest(b, c)
        if intro is None:  # meetup logged without a prior recorded thread
            intro = Introduction(connector="", b=b, c=c, week=week)
            self.intros.append(intro)
        intro.met = True
        if friended and sorted([b, c]) not in intro.friended:
            intro.friended.append(sorted([b, c]))
        self.save()

    def _find(self, b: str, c: str, week: date) -> Introduction | None:
        p = _pair(b, c)
        for i in self.intros:
            if i.pair == p and i.week == week:
                return i
        return None

    def _latest(self, b: str, c: str) -> Introduction | None:
        p = _pair(b, c)
        cand = [i for i in self.intros if i.pair == p]
        return max(cand, key=lambda i: i.week) if cand else None

    # -- signals consumed by the matcher ---------------------------------- #

    def met_pairs(self) -> set[frozenset]:
        return {i.pair for i in self.intros if i.met}

    def rested_pairs(self, current_week: date, cooldown_weeks: int) -> set[frozenset]:
        """Pairs introduced within the cooldown that did NOT meet."""
        rested: set[frozenset] = set()
        for i in self.intros:
            if i.met:
                continue
            weeks_ago = (current_week - i.week).days / 7
            if 0 <= weeks_ago <= cooldown_weeks:
                rested.add(i.pair)
        return rested

    def connector_success(self, zid: str) -> int:
        """How many of this connector's introductions led to a real meetup."""
        return sum(1 for i in self.intros if i.connector == zid and i.met)

    def friend_edges(self) -> list[tuple[str, str]]:
        seen: set[frozenset] = set()
        edges: list[tuple[str, str]] = []
        for i in self.intros:
            for x, y in i.friended:
                if _pair(x, y) not in seen:
                    seen.add(_pair(x, y))
                    edges.append((x, y))
        return edges


def apply_friend_adds(graph: SocialGraph, store: FeedbackStore) -> list[tuple[str, str]]:
    """Push every recorded friend-add into the live graph. Returns the edges."""
    edges = store.friend_edges()
    for a, b in edges:
        graph.add_friendship(a, b)
    return edges
