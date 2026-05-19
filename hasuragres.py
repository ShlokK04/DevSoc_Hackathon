"""
Real DevSoc Hasuragres GraphQL client.

Endpoint (public, no auth for reads):
    https://graphql.csesoc.app/v1/graphql

Schema verified against DevSoc's own production code:
  * Notangles  -> `courses { course_code course_name classes { ... times {...} } }`
  * Freerooms  -> `buildings { id name lat long aliases rooms { ... } }`
                  `rooms { ... bookings(where:{start,end}) { name bookingType start end } }`

IMPORTANT — faculty gap
-----------------------
The CampusThread brief assumed a Courses API with a `faculty` field for the
icebreaker/ranking tiebreak. The real `courses` type only has `course_code`
and `course_name` (DevSoc README: course info is "COMING SOON"). So faculty
is derived from the 4-letter subject prefix of the course code via a curated
map (`SUBJECT_FACULTY`). This is a documented proxy, not authoritative data:
"both taking COMP courses" is always true and is a perfectly good icebreaker.

Transport is injectable so the client is unit-testable offline. The default
transport uses only the standard library (no extra dependency to flake during
a hackathon).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable

from .models import Course

HASURAGRES_ENDPOINT = "https://graphql.csesoc.app/v1/graphql"

# Best-effort UNSW subject-prefix -> faculty. Not exhaustive; unknown prefixes
# fall back to the prefix itself so the icebreaker still works ("both in COMP").
SUBJECT_FACULTY: dict[str, str] = {
    "COMP": "Engineering",
    "SENG": "Engineering",
    "ELEC": "Engineering",
    "MECH": "Engineering",
    "CVEN": "Engineering",
    "MTRN": "Engineering",
    "BINF": "Engineering",
    "DESN": "Engineering",
    "MATH": "Science",
    "PHYS": "Science",
    "CHEM": "Science",
    "BIOL": "Science",
    "PSYC": "Science",
    "GEOS": "Science",
    "ACCT": "Business",
    "FINS": "Business",
    "ECON": "Business",
    "MGMT": "Business",
    "MARK": "Business",
    "COMM": "Business",
    "ARTS": "Arts, Design and Architecture",
    "SOCW": "Arts, Design and Architecture",
    "MDIA": "Arts, Design and Architecture",
    "ADAD": "Arts, Design and Architecture",
    "LAWS": "Law and Justice",
    "JURD": "Law and Justice",
    "MED": "Medicine and Health",
    "MEDS": "Medicine and Health",
    "HESC": "Medicine and Health",
}


def subject_prefix(course_code: str) -> str:
    """'COMP1531' -> 'COMP'. UNSW codes are 4 letters + 4 digits."""
    return "".join(ch for ch in course_code[:4] if ch.isalpha()).upper()


def faculty_for_code(course_code: str) -> str:
    prefix = subject_prefix(course_code)
    return SUBJECT_FACULTY.get(prefix, prefix)


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #

# A transport takes (query, variables) and returns the parsed `data` object.
Transport = Callable[[str, dict], dict]


def _urllib_transport(endpoint: str, timeout: float) -> Transport:
    def _send(query: str, variables: dict) -> dict:
        body = json.dumps({"query": query, "variables": variables}).encode()
        req = urllib.request.Request(
            endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
        if payload.get("errors"):
            raise RuntimeError(f"GraphQL errors: {payload['errors']}")
        return payload.get("data", {})

    return _send


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #

_COURSE_QUERY = """
query Course($code: String!) {
  courses(where: { course_code: { _eq: $code } }, limit: 1) {
    course_code
    course_name
  }
}
"""

_BOOKINGS_QUERY = """
query Bookings($start: timestamptz, $end: timestamptz) {
  rooms {
    id
    name
    bookings(where: { start: { _lte: $end }, end: { _gte: $start } },
             order_by: { start: asc }) {
      name
      bookingType
      start
      end
    }
  }
}
"""

_BUILDINGS_QUERY = """
query Buildings {
  buildings(order_by: { name: asc }) {
    id
    name
    lat
    long
    aliases
    rooms(order_by: { id: asc }) {
      id
      name
      abbr
      school
      usage
      capacity
    }
  }
}
"""

# Buildings + rooms + only the bookings that overlap a given window, in one
# round trip. Used by the room resolver to find spaces free during a meetup.
_ROOMS_WITH_BOOKINGS_QUERY = """
query RoomsWithBookings($start: timestamptz, $end: timestamptz) {
  buildings(order_by: { name: asc }) {
    id
    name
    lat
    long
    aliases
    rooms(order_by: { id: asc }) {
      id
      name
      usage
      capacity
      bookings(where: { start: { _lte: $end }, end: { _gte: $start } },
               order_by: { start: asc }) {
        name
        bookingType
        start
        end
      }
    }
  }
}
"""


class HasuragresClient:
    """Thin GraphQL client over the public Hasuragres endpoint."""

    def __init__(
        self,
        endpoint: str = HASURAGRES_ENDPOINT,
        timeout: float = 10.0,
        transport: Transport | None = None,
    ):
        self.endpoint = endpoint
        self._transport = transport or _urllib_transport(endpoint, timeout)

    def query(self, gql: str, variables: dict | None = None) -> dict:
        return self._transport(gql, variables or {})

    # -- buildings / rooms / bookings (meetup-room resolution) -------------- #

    def buildings(self) -> list[dict]:
        return self.query(_BUILDINGS_QUERY).get("buildings", [])

    def bookings_in_range(self, start_iso: str, end_iso: str) -> list[dict]:
        return self.query(
            _BOOKINGS_QUERY, {"start": start_iso, "end": end_iso}
        ).get("rooms", [])

    def rooms_with_bookings(self, start_iso: str, end_iso: str) -> list[dict]:
        """Buildings -> rooms -> bookings overlapping [start, end], one query."""
        return self.query(
            _ROOMS_WITH_BOOKINGS_QUERY, {"start": start_iso, "end": end_iso}
        ).get("buildings", [])


class HasuragresCoursesClient:
    """
    Implements the `UnswCoursesClient` protocol the matcher depends on.

    `get_course` returns a `Course`. Because the API has no faculty/school,
    `faculty` is the prefix-derived proxy and `school` is left empty. Results
    are cached per code (the Sunday matcher reuses codes heavily) and the
    client degrades gracefully: if the network/endpoint is unavailable the
    course name falls back to the code, but the faculty proxy still works.
    """

    def __init__(self, client: HasuragresClient | None = None):
        self._client = client or HasuragresClient()
        self._cache: dict[str, Course | None] = {}

    def get_course(self, course_code: str) -> Course | None:
        code = course_code.upper()
        if code in self._cache:
            return self._cache[code]

        course_name = code
        try:
            data = self._client.query(_COURSE_QUERY, {"code": code})
            rows = data.get("courses", [])
            if rows and rows[0].get("course_name"):
                course_name = rows[0]["course_name"]
        except (urllib.error.URLError, RuntimeError, TimeoutError, OSError):
            # Offline / endpoint down: keep the faculty proxy, degrade name.
            pass

        course = Course(
            course_code=code,
            course_name=course_name,
            faculty=faculty_for_code(code),
            school="",  # not available from this API
        )
        self._cache[code] = course
        return course
