"""
End-to-end demo.

    python -m campusthread.demo

Part 1 — the Sunday pipeline: parse .ics, match, resolve a real room,
          generate the icebreaker.
Part 2 — the We-met feedback loop: the thread's members meet and add each
          other; the store persists to JSON; matching is re-run and the
          network has visibly changed.

Tries the live DevSoc GraphQL endpoint first; falls back to an offline
fixture so the whole pipeline (incl. rooms) is demonstrable either way.
"""

from __future__ import annotations

import json
import tempfile
from datetime import date
from pathlib import Path

from .feedback import FeedbackStore, apply_friend_adds
from .hasuragres import HasuragresClient, HasuragresCoursesClient, _COURSE_QUERY
from .icebreaker import generate_icebreaker
from .ics_parser import parse_ics_file
from .matching import run_weekly_matching
from .models import User
from .room_resolver import enrich_match_with_rooms
from .social_graph import SocialGraph

DATA = Path(__file__).parent / "sample_data"
WEEK_START = date(2026, 5, 25)


def _fixture_transport():
    fx = json.loads((DATA / "hasura_fixture.json").read_text())

    def _send(query: str, variables: dict) -> dict:
        if "course_name" in query:
            code = (variables.get("code") or "").upper()
            return {"courses": [{"course_code": code,
                                 "course_name": fx["courses"].get(code, code)}]}
        if "bookings" in query:
            return {"buildings": fx["buildings"]}
        return {}

    return _send


def _build_client() -> tuple[HasuragresClient, str]:
    live = HasuragresClient()
    try:
        if live.query(_COURSE_QUERY, {"code": "COMP1531"}).get("courses"):
            return live, "LIVE (graphql.csesoc.app)"
    except Exception:
        pass
    return HasuragresClient(transport=_fixture_transport()), "OFFLINE (fixture)"


def _load(client: HasuragresClient):
    world = json.loads((DATA / "world.json").read_text())
    users = {
        r["zid"]: User(r["zid"], r["name"], r["course_codes"],
                       parse_ics_file(DATA / r["ics"]))
        for r in world["users"]
    }
    graph = SocialGraph()
    for zid in users:
        graph.add_user(zid)
    for a, b in world["friendships"]:
        graph.add_friendship(a, b)
    return users, graph, HasuragresCoursesClient(client)


def _name(users, zid):
    return users[zid].name


def _print_chat(m, users, client, indent="    "):
    enrich_match_with_rooms(m, client)
    days = ", ".join(d.strftime("%a %d %b") for d in m.shared_campus_days)
    print(f"{indent}{m.connector.name} (connector) ▸ "
          f"{m.member_b.name} ▸ {m.member_c.name}")
    print(f"{indent}  shared campus : {days}   faculty: {m.shared_faculty or '—'}")
    for w, room in zip(m.meetup_windows, m.room_suggestions):
        tail = f"  →  {room.label()}" if room else "  →  (no free room)"
        print(f"{indent}  {w.label()}{tail}")
    print(f"{indent}  icebreaker: {generate_icebreaker(m)}")


def _pairs(graph):
    return {frozenset((b, c)) for _, b, c in graph.candidate_triplets()}


def main() -> None:
    client, mode = _build_client()
    users, graph, courses = _load(client)

    print("CampusThread — end-to-end demo")
    print(f"Week of Monday {WEEK_START.isoformat()}   ·   DevSoc API: {mode}")

    # ---- Part 1 : the Sunday pipeline ---------------------------------- #
    print("\n" + "─" * 64)
    print("PART 1 — Sunday matching + room resolution")
    print("─" * 64)
    week1 = run_weekly_matching(users, graph, courses, WEEK_START)
    if not week1:
        print("  No valid triplets.")
        return
    print(f"\n{len(week1)} thread(s) created:\n")
    for m in week1:
        _print_chat(m, users, client)
        print()

    headline = week1[0]
    b, c = headline.member_b.zid, headline.member_c.zid

    # ---- Part 2 : the We-met feedback loop ----------------------------- #
    print("─" * 64)
    print("PART 2 — the 'We met' feedback loop")
    print("─" * 64)

    store_path = Path(tempfile.gettempdir()) / "campusthread_feedback.json"
    store_path.unlink(missing_ok=True)              # deterministic demo
    store = FeedbackStore(store_path)

    store.record_introductions(week1, WEEK_START)
    print(f"\n  Logged thread to {store_path.name} (status: pending).")

    print(f"  → {_name(users,b)} and {_name(users,c)} tap “We met” "
          f"and add each other as friends.")
    store.record_we_met(b, c, WEEK_START, friended=True)

    # Prove persistence: reload the store fresh from disk.
    reloaded = FeedbackStore(store_path)
    rec = reloaded.intros[0]
    print(f"  Reloaded from disk → met={rec.met}, friended={rec.friended}, "
          f"connector {_name(users,rec.connector)} credited.")

    pairs_before = _pairs(graph)
    edges = apply_friend_adds(graph, reloaded)
    pairs_after = _pairs(graph)
    unlocked = pairs_after - pairs_before
    closed = pairs_before - pairs_after

    print(f"\n  Graph mutated: added edge "
          f"{' , '.join(_name(users,x)+'–'+_name(users,y) for x,y in edges)}")

    def fmt(ps):
        return ", ".join(
            "{" + "–".join(sorted(_name(users, z) for z in p)) + "}" for p in ps
        ) or "none"

    print(f"  Warm-intros now closed   : {fmt(closed)}  (they know each other)")
    print(f"  Warm-intros now unlocked : {fmt(unlocked)}  "
          f"(new second-degree reach)")

    print("\n  Re-running the SAME Sunday with feedback applied")
    print("  (same week, to isolate the loop's effect):\n")
    week2 = run_weekly_matching(
        users, graph, courses, WEEK_START, feedback=reloaded
    )
    if not week2:
        print("    No valid triplets.")
    for m in week2:
        _print_chat(m, users, client)
        print()

    j = headline.connector
    print(f"  Why it changed: the {_name(users,b)}–{_name(users,c)} thread "
          f"can't recur (they met).")
    if week2:
        n = week2[0]
        print(f"  Because they connected, {n.connector.name} can now introduce "
              f"{n.member_b.name} to {n.member_c.name} — a pair with no path "
              f"before.")
    print(f"  And {j.name}'s proven matchmaking "
          f"({reloaded.connector_success(j.zid)} successful intro) is a "
          f"tie-break nudge for future threads.")
    print("\n  → " + '"One real meetup between two people who would never '
          'have otherwise met."')


if __name__ == "__main__":
    main()
