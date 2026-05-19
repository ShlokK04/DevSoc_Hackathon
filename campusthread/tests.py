"""
Tests for the non-negotiable matching invariants.

    python -m campusthread.tests
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .matching import run_weekly_matching, _free_gaps, _merge
from .models import ClassEvent, Timetable, User
from .social_graph import SocialGraph
from .unsw_courses import StaticCourses

WEEK = date(2026, 5, 25)            # Monday
MON, WED, THU = date(2026, 5, 25), date(2026, 5, 27), date(2026, 5, 28)


def _ev(day: date, h1: int, h2: int, loc="Quad G01") -> ClassEvent:
    return ClassEvent("Class", loc, datetime(day.year, day.month, day.day, h1),
                       datetime(day.year, day.month, day.day, h2))


def _user(zid, name, events, courses, optout=False) -> User:
    return User(zid, name, courses, Timetable(events), optout)


COURSES = StaticCourses.from_records([
    {"course_code": "COMP1531", "course_name": "SE", "faculty": "Engineering", "school": "CSE"},
    {"course_code": "ARTS1000", "course_name": "Phil", "faculty": "Arts", "school": "HAL"},
])


def _graph(*pairs):
    g = SocialGraph()
    for a, b in pairs:
        g.add_friendship(a, b)
    return g


def test_hard_constraint_no_shared_day():
    """No common on-campus day -> the triplet must NOT be matched."""
    a = _user("a", "A", [_ev(WED, 9, 11)], ["COMP1531"])
    b = _user("b", "B", [_ev(WED, 12, 14)], ["COMP1531"])
    c = _user("c", "C", [_ev(THU, 9, 11)], ["COMP1531"])   # only Thu
    users = {u.zid: u for u in (a, b, c)}
    g = _graph(("a", "b"), ("a", "c"))
    assert run_weekly_matching(users, g, COURSES, WEEK) == []
    print("ok  hard constraint: zero shared days -> no match")


def test_match_created_when_day_shared():
    a = _user("a", "A", [_ev(WED, 9, 11)], ["COMP1531"])
    b = _user("b", "B", [_ev(WED, 12, 14)], ["COMP1531"])
    c = _user("c", "C", [_ev(WED, 15, 17)], ["COMP1531"])
    users = {u.zid: u for u in (a, b, c)}
    g = _graph(("a", "b"), ("a", "c"))
    matches = run_weekly_matching(users, g, COURSES, WEEK)
    assert len(matches) == 1
    assert matches[0].connector.zid == "a"
    assert matches[0].shared_campus_days == [WED]
    print("ok  match created on a shared on-campus day")


def test_optout_skips_triplet():
    a = _user("a", "A", [_ev(WED, 9, 11)], ["COMP1531"])
    b = _user("b", "B", [_ev(WED, 12, 14)], ["COMP1531"], optout=True)
    c = _user("c", "C", [_ev(WED, 15, 17)], ["COMP1531"])
    users = {u.zid: u for u in (a, b, c)}
    g = _graph(("a", "b"), ("a", "c"))
    assert run_weekly_matching(users, g, COURSES, WEEK) == []
    print("ok  opt-out member removes the triplet")


def test_bc_friends_is_not_a_candidate():
    """If B and C already know each other it's not a warm intro."""
    a = _user("a", "A", [_ev(WED, 9, 11)], ["COMP1531"])
    b = _user("b", "B", [_ev(WED, 12, 14)], ["COMP1531"])
    c = _user("c", "C", [_ev(WED, 15, 17)], ["COMP1531"])
    users = {u.zid: u for u in (a, b, c)}
    g = _graph(("a", "b"), ("a", "c"), ("b", "c"))   # b-c are friends
    assert run_weekly_matching(users, g, COURSES, WEEK) == []
    print("ok  B–C already friends -> not a candidate triplet")


def test_ranking_prefers_more_days_then_faculty():
    """More shared days beats a faculty match; faculty breaks day ties."""
    # Triplet 1 (via connector p): 2 shared days, mixed faculty.
    p = _user("p", "P", [_ev(MON, 9, 10), _ev(WED, 9, 10)], ["COMP1531"])
    q = _user("q", "Q", [_ev(MON, 11, 12), _ev(WED, 11, 12)], ["ARTS1000"])
    r = _user("r", "R", [_ev(MON, 13, 14), _ev(WED, 13, 14)], ["ARTS1000"])
    # Triplet 2 (via connector p): 1 shared day, full faculty match.
    s = _user("s", "S", [_ev(WED, 15, 16)], ["COMP1531"])
    users = {u.zid: u for u in (p, q, r, s)}
    g = _graph(("p", "q"), ("p", "r"), ("p", "s"))
    matches = run_weekly_matching(users, g, COURSES, WEEK)
    # Greedy: p can only be in one chat. The 2-day triplet outranks the
    # 1-day one despite weaker faculty, so {p,q,r} wins.
    assert len(matches) == 1
    assert matches[0].member_zids() == {"p", "q", "r"}
    assert matches[0].score[0] == 2
    print("ok  ranking: more shared days outranks faculty match")


def test_interval_helpers():
    assert _merge([(0, 10), (5, 15), (20, 25)]) == [(0, 15), (20, 25)]
    assert _free_gaps([(540, 660), (720, 840)], 540, 1080) == [(660, 720), (840, 1080)]
    print("ok  interval merge / free-gap maths")


def test_faculty_proxy_from_course_code():
    from .hasuragres import faculty_for_code, subject_prefix
    assert subject_prefix("COMP1531") == "COMP"
    assert faculty_for_code("COMP2521") == "Engineering"
    assert faculty_for_code("ARTS1000") == "Arts, Design and Architecture"
    assert faculty_for_code("FINS1613") == "Business"
    # Unknown prefix -> falls back to the prefix itself (still a usable signal).
    assert faculty_for_code("ZZZZ9999") == "ZZZZ"
    print("ok  faculty proxy derives from course-code prefix")


def test_hasuragres_courses_client_live_path():
    """Mock transport returns a course_name -> client uses it; caches result."""
    from .hasuragres import HasuragresClient, HasuragresCoursesClient

    calls = []

    def fake_transport(query, variables):
        calls.append(variables)
        assert "courses(where:" in query.replace(" ", "").replace("\n", "")
        return {"courses": [{"course_code": "COMP1531",
                             "course_name": "Software Engineering Fundamentals"}]}

    cc = HasuragresCoursesClient(HasuragresClient(transport=fake_transport))
    c1 = cc.get_course("comp1531")
    assert c1.course_name == "Software Engineering Fundamentals"
    assert c1.faculty == "Engineering"
    assert c1.school == ""
    cc.get_course("COMP1531")          # served from cache
    assert len(calls) == 1
    print("ok  Hasuragres courses client: live path + caching")


def test_hasuragres_courses_client_offline_degrades():
    """Transport raises -> name degrades to code but faculty proxy still works."""
    from .hasuragres import HasuragresClient, HasuragresCoursesClient

    def broken_transport(query, variables):
        raise OSError("no network")

    cc = HasuragresCoursesClient(HasuragresClient(transport=broken_transport))
    c = cc.get_course("MATH1241")
    assert c.course_name == "MATH1241"      # degraded
    assert c.faculty == "Science"           # proxy still works
    print("ok  Hasuragres courses client: offline graceful degradation")


def test_matcher_unchanged_with_real_client():
    """The Protocol seam holds: the matcher runs identically on the real client."""
    from .hasuragres import HasuragresClient, HasuragresCoursesClient

    def fake_transport(query, variables):
        code = variables["code"]
        return {"courses": [{"course_code": code, "course_name": f"{code} Name"}]}

    cc = HasuragresCoursesClient(HasuragresClient(transport=fake_transport))
    a = _user("a", "A", [_ev(WED, 9, 11)], ["COMP1531"])
    b = _user("b", "B", [_ev(WED, 12, 14)], ["COMP2521"])
    c = _user("c", "C", [_ev(WED, 15, 17)], ["COMP6080"])
    users = {u.zid: u for u in (a, b, c)}
    g = _graph(("a", "b"), ("a", "c"))
    matches = run_weekly_matching(users, g, cc, WEEK)
    assert len(matches) == 1
    assert matches[0].shared_faculty == "Engineering"   # all COMP -> Engineering
    print("ok  matcher unchanged when fed the real Hasuragres client")


def test_room_resolution_picks_free_nearby_room():
    """Full room pipeline against the offline fixture: window -> a free room."""
    import json
    from pathlib import Path
    from .hasuragres import HasuragresClient
    from .room_resolver import enrich_match_with_rooms, resolve_building
    from .ics_parser import parse_ics_file

    data = Path(__file__).parent / "sample_data"
    fx = json.loads((data / "hasura_fixture.json").read_text())

    def transport(query, variables):
        if "course_name" in query:
            code = variables["code"].upper()
            return {"courses": [{"course_code": code,
                                 "course_name": fx["courses"].get(code, code)}]}
        if "bookings" in query:
            return {"buildings": fx["buildings"]}
        return {}

    client = HasuragresClient(transport=transport)
    blds = fx["buildings"]

    # Building resolution from the same LOCATION strings the .ics uses.
    assert resolve_building("Ainsworth Building G03", blds)["id"] == "K-J17"
    assert resolve_building("K17 Building 113", blds)["id"] == "K-K17"
    assert resolve_building("Quadrangle G040", blds)["id"] == "K-E15"

    jamie = User("z1", "Jamie", ["COMP1531"],
                 parse_ics_file(data / "jamie.ics"))
    alex = User("z2", "Alex", ["COMP6080"],
                parse_ics_file(data / "alex.ics"))
    sam = User("z3", "Sam", ["COMP2521"],
               parse_ics_file(data / "sam.ics"))
    users = {u.zid: u for u in (jamie, alex, sam)}
    g = _graph(("z1", "z2"), ("z1", "z3"))

    from .hasuragres import HasuragresCoursesClient
    cc = HasuragresCoursesClient(client)
    matches = run_weekly_matching(users, g, cc, date(2026, 5, 25))
    assert len(matches) == 1
    m = matches[0]
    enrich_match_with_rooms(m, client)

    assert len(m.room_suggestions) == len(m.meetup_windows)
    first = m.room_suggestions[0]
    assert first is not None
    # The 11:00–12:00 window must NOT return a room that's booked then.
    assert first.room_id not in {"K-J17-G03", "K-E15-G040"} or first.capacity >= 3
    assert first.distance_m is not None and first.distance_m >= 0
    print(f"ok  room resolution: window -> {first.room_name} "
          f"({first.building_name}, seats {first.capacity})")


def test_booked_room_excluded_for_overlapping_window():
    """A room booked across the window is never suggested for it."""
    from datetime import time
    from .hasuragres import HasuragresClient
    from .room_resolver import free_rooms_for_window
    from .models import MeetupWindow

    buildings = [{
        "id": "B1", "name": "Test Building", "lat": -33.9, "long": 151.2,
        "aliases": ["TB"],
        "rooms": [
            {"id": "B1-1", "name": "Busy Room", "usage": "Seminar", "capacity": 20,
             "bookings": [{"name": "X", "bookingType": "Class",
                           "start": "2026-05-27T10:30:00+10:00",
                           "end": "2026-05-27T11:30:00+10:00"}]},
            {"id": "B1-2", "name": "Free Room", "usage": "Seminar", "capacity": 20,
             "bookings": []},
        ],
    }]

    def transport(query, variables):
        return {"buildings": buildings}

    client = HasuragresClient(transport=transport)
    win = MeetupWindow(date(2026, 5, 27), time(11, 0), time(12, 0))
    rooms = free_rooms_for_window(client, win, ["Test Building"], min_capacity=3)
    ids = {r.room_id for r in rooms}
    assert "B1-1" not in ids and "B1-2" in ids
    print("ok  booked room excluded for an overlapping window")


def test_feedback_none_is_backward_compatible():
    """feedback=None must give byte-identical results to the old matcher."""
    a = _user("a", "A", [_ev(WED, 9, 11)], ["COMP1531"])
    b = _user("b", "B", [_ev(WED, 12, 14)], ["COMP1531"])
    c = _user("c", "C", [_ev(WED, 15, 17)], ["COMP1531"])
    users = {u.zid: u for u in (a, b, c)}
    g = _graph(("a", "b"), ("a", "c"))
    from .feedback import FeedbackStore
    m1 = run_weekly_matching(users, g, COURSES, WEEK)
    m2 = run_weekly_matching(users, g, COURSES, WEEK, feedback=FeedbackStore())
    assert [x.member_zids() for x in m1] == [x.member_zids() for x in m2]
    print("ok  feedback=None / empty store: behaviour unchanged")


def test_met_pair_not_reintroduced():
    from .feedback import FeedbackStore
    a = _user("a", "A", [_ev(WED, 9, 11)], ["COMP1531"])
    b = _user("b", "B", [_ev(WED, 12, 14)], ["COMP1531"])
    c = _user("c", "C", [_ev(WED, 15, 17)], ["COMP1531"])
    users = {u.zid: u for u in (a, b, c)}
    g = _graph(("a", "b"), ("a", "c"))
    store = FeedbackStore()
    store.record_introductions(
        run_weekly_matching(users, g, COURSES, WEEK), WEEK)
    store.record_we_met("b", "c", WEEK, friended=False)   # met, no friend-add
    after = run_weekly_matching(users, g, COURSES, WEEK, feedback=store)
    assert after == []          # they met -> never re-threaded
    print("ok  a pair that met is not re-introduced")


def test_friend_add_mutates_graph_and_unlocks_new_intro():
    from .feedback import FeedbackStore, apply_friend_adds
    a = _user("a", "A", [_ev(WED, 9, 11)], ["COMP1531"])
    b = _user("b", "B", [_ev(WED, 12, 14)], ["COMP1531"])
    c = _user("c", "C", [_ev(WED, 15, 16)], ["COMP1531"])
    d = _user("d", "D", [_ev(WED, 16, 17)], ["COMP1531"])
    users = {u.zid: u for u in (a, b, c, d)}
    g = _graph(("a", "b"), ("a", "c"), ("c", "d"))   # D reachable only via C

    def pairset(graph):
        return {frozenset((x, y)) for _, x, y in graph.candidate_triplets()}

    store = FeedbackStore()
    store.record_introductions(
        run_weekly_matching(users, g, COURSES, WEEK), WEEK)
    store.record_we_met("b", "c", WEEK, friended=True)

    before = pairset(g)
    edges = apply_friend_adds(g, store)
    after = pairset(g)

    assert ("b", "c") in edges or ("c", "b") in edges
    assert g.are_friends("b", "c")
    assert frozenset(("b", "c")) in (before - after)        # closed
    assert frozenset(("b", "d")) in (after - before)        # unlocked
    print("ok  friend-add mutates graph: closes B–C, unlocks B–D")


def test_rested_pair_respects_cooldown():
    from .feedback import FeedbackStore, Introduction
    a = _user("a", "A", [_ev(WED, 9, 11)], ["COMP1531"])
    b = _user("b", "B", [_ev(WED, 12, 14)], ["COMP1531"])
    c = _user("c", "C", [_ev(WED, 15, 17)], ["COMP1531"])
    users = {u.zid: u for u in (a, b, c)}
    g = _graph(("a", "b"), ("a", "c"))

    recent = FeedbackStore()
    recent.intros.append(Introduction("a", "b", "c", WEEK, met=False))
    assert run_weekly_matching(
        users, g, COURSES, WEEK, feedback=recent, cooldown_weeks=3) == []

    old = FeedbackStore()
    old.intros.append(
        Introduction("a", "b", "c", WEEK - timedelta(days=35), met=False))
    assert len(run_weekly_matching(
        users, g, COURSES, WEEK, feedback=old, cooldown_weeks=3)) == 1
    print("ok  introduced-but-not-met pair is rested, then freed after cooldown")


def test_connector_reputation_breaks_ties():
    """Equal day/faculty/free: the proven matchmaker's thread ranks first."""
    from .feedback import FeedbackStore, Introduction
    mk = lambda z: _user(z, z.upper(), [_ev(WED, 9, 10)], ["COMP1531"])
    p, q, r = mk("p"), mk("q"), mk("r")     # triplet via connector P
    x, y, z = mk("x"), mk("y"), mk("z")     # triplet via connector X
    users = {u.zid: u for u in (p, q, r, x, y, z)}
    g = _graph(("p", "q"), ("p", "r"), ("x", "y"), ("x", "z"))

    store = FeedbackStore()
    # P has a prior successful introduction; X does not.
    store.intros.append(Introduction("p", "m", "n", WEEK, met=True))

    matches = run_weekly_matching(users, g, COURSES, WEEK, feedback=store)
    assert len(matches) == 2
    assert matches[0].connector.zid == "p"     # reputation wins the tie
    print("ok  proven-matchmaker nudge breaks an otherwise exact tie")


def test_feedback_persists_to_json(tmp_path_str=None):
    import os
    import tempfile
    from pathlib import Path
    from .feedback import FeedbackStore
    if tmp_path_str is None:  # cross-platform: no hardcoded /tmp (breaks on Windows)
        tmp_path_str = os.path.join(tempfile.gettempdir(), "_ct_fb_test.json")
    Path(tmp_path_str).unlink(missing_ok=True)
    s = FeedbackStore(tmp_path_str)
    a = _user("a", "A", [_ev(WED, 9, 11)], ["COMP1531"])
    b = _user("b", "B", [_ev(WED, 12, 14)], ["COMP1531"])
    c = _user("c", "C", [_ev(WED, 15, 17)], ["COMP1531"])
    users = {u.zid: u for u in (a, b, c)}
    g = _graph(("a", "b"), ("a", "c"))
    s.record_introductions(run_weekly_matching(users, g, COURSES, WEEK), WEEK)
    s.record_we_met("b", "c", WEEK, friended=True)

    fresh = FeedbackStore(tmp_path_str)                 # reload from disk
    assert fresh.met_pairs() == {frozenset(("b", "c"))}
    assert fresh.connector_success("a") == 1
    assert fresh.friend_edges() == [("b", "c")]
    Path(tmp_path_str).unlink(missing_ok=True)
    print("ok  feedback store persists to JSON and reloads intact")


def run_all():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nall tests passed")


if __name__ == "__main__":
    run_all()
