"""
The social graph.

This is the foundation of all matching: no friends, no matches. It answers
two questions for the matcher:

  * who are X's friends?
  * which (A, B, C) triplets are valid candidates — i.e. A is friends with
    both B and C, but B and C are NOT friends with each other?

A is the *connector*: the mutual friend who makes the introduction warm.
"""

from __future__ import annotations

from itertools import combinations
from typing import Iterator


class SocialGraph:
    def __init__(self) -> None:
        self._adj: dict[str, set[str]] = {}

    def add_user(self, zid: str) -> None:
        self._adj.setdefault(zid, set())

    def add_friendship(self, a: str, b: str) -> None:
        if a == b:
            return
        self._adj.setdefault(a, set()).add(b)
        self._adj.setdefault(b, set()).add(a)

    def friends(self, zid: str) -> set[str]:
        return set(self._adj.get(zid, set()))

    def are_friends(self, a: str, b: str) -> bool:
        return b in self._adj.get(a, set())

    def second_degree(self, zid: str) -> set[str]:
        """Friends-of-friends who are not already friends with `zid`."""
        own = self._adj.get(zid, set())
        result: set[str] = set()
        for f in own:
            for ff in self._adj.get(f, set()):
                if ff != zid and ff not in own:
                    result.add(ff)
        return result

    def candidate_triplets(self) -> Iterator[tuple[str, str, str]]:
        """
        Yield (connector, b, c) such that:
          connector–b are friends, connector–c are friends,
          b–c are NOT friends, and all three are distinct.

        b < c (by zid) is enforced so each unordered pair is yielded once
        per connector. Different connectors for the same pair are distinct
        candidates on purpose: the icebreaker differs by mutual friend.
        """
        for connector, fset in self._adj.items():
            for b, c in combinations(sorted(fset), 2):
                if not self.are_friends(b, c):
                    yield (connector, b, c)
