"""
Room resolution.

The matcher hands us `MeetupWindow`s — a day and a time gap when all three
members are free and on campus. This layer turns a window into an actual
bookable space, using the verified Freerooms schema:

    buildings { id name lat long aliases
                rooms { id name usage capacity
                        bookings(where:{overlap}) { name bookingType start end } } }

Approach:
  * The trio is already somewhere on campus that day (their class locations
    from the .ics). We resolve those location strings to buildings and take
    the centroid as the "anchor" — the place to stay near.
  * A room is a candidate if NOTHING is booked over the whole window.
  * Candidates are ranked: nearest to the anchor first, then smallest room
    that still fits (a 6-seat meeting room beats a 350-seat theatre for
    three people).

Network-dependent, so this is deliberately separate from `matching.py`,
which stays offline-safe. `enrich_match_with_rooms` is the entry point the
chat-creation step calls.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone

from .hasuragres import HasuragresClient
from .models import Match, MeetupWindow, RoomSuggestion, User

# UNSW is Australia/Sydney. Prefer the real zone (handles AEST/AEDT); fall
# back to a fixed +10:00 if the tz database isn't bundled.
try:
    from zoneinfo import ZoneInfo

    _SYD = ZoneInfo("Australia/Sydney")
except Exception:  # pragma: no cover - depends on host tzdata
    _SYD = timezone(timedelta(hours=10))


# --------------------------------------------------------------------------- #
# Geo + time helpers
# --------------------------------------------------------------------------- #

def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in metres. Fine as a campus walking proxy."""
    r = 6_371_000.0
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _syd_iso(d: date, t: time) -> str:
    return datetime(d.year, d.month, d.day, t.hour, t.minute, tzinfo=_SYD).isoformat()


def _to_local_naive(iso: str) -> datetime | None:
    """Parse an API timestamp into naive Sydney local time for comparison."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(_SYD).replace(tzinfo=None)
    return dt


def _overlaps(b_start: str, b_end: str, win_start: datetime, win_end: datetime) -> bool:
    s = _to_local_naive(b_start)
    e = _to_local_naive(b_end)
    if s is None or e is None:
        return True  # unparseable booking -> treat as busy (safe default)
    return s < win_end and e > win_start


# --------------------------------------------------------------------------- #
# Building resolution
# --------------------------------------------------------------------------- #

def _norm(s: str) -> str:
    return "".join(ch.lower() for ch in s if ch.isalnum() or ch == " ").strip()


def resolve_building(location: str, buildings: list[dict]) -> dict | None:
    """
    Best-effort match of a .ics LOCATION string to a building.

    Tries, in order: a room whose name is a prefix of the location, a
    building whose name appears in the location, then any alias token.
    """
    loc = _norm(location)
    if not loc:
        return None

    for bld in buildings:  # exact-ish: a room name leads the location string
        for room in bld.get("rooms", []):
            rn = _norm(room.get("name", ""))
            if rn and (loc.startswith(rn) or rn in loc):
                return bld

    for bld in buildings:  # building name contained in the location
        bn = _norm(bld.get("name", ""))
        if bn and bn.split()[0] in loc and bn[:5] in loc:
            return bld

    for bld in buildings:  # alias token match
        for alias in bld.get("aliases", []) or []:
            al = _norm(alias)
            if al and al in loc:
                return bld
    return None


def _anchor(locations: list[str], buildings: list[dict]) -> tuple[float, float] | None:
    pts: list[tuple[float, float]] = []
    for loc in locations:
        bld = resolve_building(loc, buildings)
        if bld and bld.get("lat") is not None and bld.get("long") is not None:
            pts.append((float(bld["lat"]), float(bld["long"])))
    if not pts:
        return None
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


# --------------------------------------------------------------------------- #
# Free-room search
# --------------------------------------------------------------------------- #

def free_rooms_for_window(
    client: HasuragresClient,
    window: MeetupWindow,
    anchor_locations: list[str],
    *,
    min_capacity: int = 3,
    limit: int = 3,
) -> list[RoomSuggestion]:
    """Rooms free for the whole window, ranked by nearness then right-size."""
    win_start = datetime.combine(window.day, window.start)
    win_end = datetime.combine(window.day, window.end)

    try:
        buildings = client.rooms_with_bookings(
            _syd_iso(window.day, window.start), _syd_iso(window.day, window.end)
        )
    except Exception:
        return []  # offline / endpoint down -> no rooms, windows still shown

    anchor = _anchor(anchor_locations, buildings)

    candidates: list[tuple[float, int, RoomSuggestion]] = []
    for bld in buildings:
        lat, lng = bld.get("lat"), bld.get("long")
        for room in bld.get("rooms", []):
            cap = room.get("capacity") or 0
            if cap < min_capacity:
                continue
            booked = any(
                _overlaps(bk.get("start", ""), bk.get("end", ""), win_start, win_end)
                for bk in room.get("bookings", [])
            )
            if booked:
                continue

            dist = None
            if anchor and lat is not None and lng is not None:
                dist = _haversine_m(anchor, (float(lat), float(lng)))

            candidates.append(
                (
                    dist if dist is not None else float("inf"),
                    cap,  # tiebreak: prefer the smallest room that still fits
                    RoomSuggestion(
                        building_id=bld.get("id", ""),
                        building_name=bld.get("name", ""),
                        room_id=room.get("id", ""),
                        room_name=room.get("name", ""),
                        capacity=cap,
                        usage=room.get("usage", "") or "",
                        distance_m=dist,
                    ),
                )
            )

    candidates.sort(key=lambda c: (c[0], c[1], c[2].room_name))
    return [c[2] for c in candidates[:limit]]


def _anchor_locations_for_day(members: tuple[User, ...], day: date) -> list[str]:
    """Where the trio already is that day — their on-campus class locations."""
    locs: list[str] = []
    for m in members:
        for ev in m.timetable.events_on(day):
            if ev.is_on_campus and ev.location:
                locs.append(ev.location)
    return locs


def enrich_match_with_rooms(
    match: Match, client: HasuragresClient
) -> Match:
    """Attach the best free room to each of the match's meetup windows."""
    suggestions: list[RoomSuggestion | None] = []
    for window in match.meetup_windows:
        anchors = _anchor_locations_for_day(match.members(), window.day)
        rooms = free_rooms_for_window(client, window, anchors, limit=1)
        suggestions.append(rooms[0] if rooms else None)
    match.room_suggestions = suggestions
    return match
