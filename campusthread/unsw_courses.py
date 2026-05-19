"""
The `UnswCoursesClient` seam.

`matching.py` only ever depends on this Protocol — never on a concrete
implementation. That's the whole point: the offline `StaticCourses` (used by
tests and as a fallback) and the live `HasuragresCoursesClient` are
interchangeable, so swapping the stub for the real API changed zero lines of
the algorithm.

A client answers exactly one question: given a course code, what `Course`
(code, name, faculty, school) does it map to — or `None` if unknown.
"""

from __future__ import annotations

from typing import Iterable, Protocol, runtime_checkable

from .models import Course


@runtime_checkable
class UnswCoursesClient(Protocol):
    """Anything the matcher can ask for course enrichment."""

    def get_course(self, course_code: str) -> Course | None:
        ...


class StaticCourses:
    """
    An in-memory `UnswCoursesClient`, backed by a fixed table.

    Used by the test-suite (deterministic, no network) and as the offline
    fallback. Lookups are case-insensitive on the course code.
    """

    def __init__(self, courses: dict[str, Course] | None = None):
        self._by_code: dict[str, Course] = {}
        for code, course in (courses or {}).items():
            self._by_code[code.upper()] = course

    @classmethod
    def from_records(cls, records: Iterable[dict]) -> "StaticCourses":
        """Build from a list of {course_code, course_name, faculty, school}."""
        table: dict[str, Course] = {}
        for r in records:
            code = r["course_code"].upper()
            table[code] = Course(
                course_code=code,
                course_name=r.get("course_name", code),
                faculty=r.get("faculty", ""),
                school=r.get("school", ""),
            )
        return cls(table)

    def get_course(self, course_code: str) -> Course | None:
        return self._by_code.get(course_code.upper())
