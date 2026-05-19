"""
CampusThread as a real localhost web app.

    python -m campusthread.server          # http://127.0.0.1:8000
    python -m campusthread.server 9000     # custom port

Zero third-party dependencies — stdlib `http.server` + `sqlite3` only (no
Flask, no Docker). It is a genuine multi-user application, not a scripted
demo:

  * register / login / logout with hashed passwords and server sessions
  * upload your real timetable (.ics) — parsed and stored
  * send / accept friend requests (the social graph the matcher reads)
  * run the Sunday 7 pm matching for everyone (warm intros + a cold-start
    pass for friendless students), persisted to SQLite
  * a homepage for anyone the Sunday run didn't place (no friends yet, or
    they joined late): "Find me a group now" matches them with others in
    the same situation, same shared-on-campus-day rule
  * a live group chat with persisted messages, and "We met" that actually
    feeds the graph back (closing the loop end-to-end from the browser)

Every endpoint validates input and returns a JSON error with the right
status code instead of a stack trace. The DevSoc GraphQL API is used for
course names + rooms when reachable, with the offline fixture as fallback —
exactly like demo.py.
"""

from __future__ import annotations

import json
import sys
import traceback
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .demo import WEEK_START, _build_client
from .hasuragres import HasuragresCoursesClient
from .icebreaker import generate_icebreaker
from .matching import run_open_matching, run_weekly_matching
from .room_resolver import enrich_match_with_rooms
from .seed import seed
from .store import DbFeedback, Store

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):  # pragma: no cover
    pass

UI_FILE = Path(__file__).parent / "ui" / "groupchat.html"
DB_FILE = Path(__file__).parent / "campusthread.db"
WEEK = WEEK_START
COOKIE = "ct_session"

STORE = Store(DB_FILE)
_CLIENT, _MODE = _build_client()          # GraphQL (live) or offline fixture
_COURSES = HasuragresCoursesClient(_CLIENT)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #

def _window_dicts(match) -> list[dict]:
    out = []
    for w, room in zip(match.meetup_windows, match.room_suggestions or []):
        out.append({
            "when": w.label(),
            "room": room.room_name if room else "—",
            "bldg": room.building_name if room else "no free room found",
            "seats": room.capacity if room else 0,
            "dist": round(room.distance_m) if room and room.distance_m is not None else "?",
        })
    if not match.room_suggestions:  # room resolution unavailable; still show times
        out = [{"when": w.label(), "room": "—", "bldg": "TBC",
                "seats": 0, "dist": "?"} for w in match.meetup_windows]
    return out


def _persist_match(match, week) -> int:
    enrich_match_with_rooms(match, _CLIENT)
    ib = generate_icebreaker(match)
    if match.kind == "warm-intro":
        members = [
            (match.connector.zid, "connector"),
            (match.member_b.zid, "member"),
            (match.member_c.zid, "member"),
        ]
        intro = (match.connector.zid, match.member_b.zid, match.member_c.zid, match.kind)
    else:
        members = [(m.zid, "member") for m in match.members()]
        intro = ("", match.member_b.zid, match.member_c.zid, match.kind)
    tid = STORE.save_thread(
        week, match.kind, match.shared_faculty, ib, members, _window_dicts(match)
    )
    STORE.record_introductions([intro], week)
    return tid


def run_sunday() -> dict:
    """The weekly 7 pm job. Idempotent: rebuilds this week from scratch."""
    users, graph = STORE.load_world()
    STORE.clear_week(WEEK)

    warm = run_weekly_matching(
        users, graph, _COURSES, WEEK, feedback=DbFeedback(STORE)
    )
    friendless = {z: u for z, u in users.items() if not graph.friends(z)}
    cold = run_open_matching(friendless, _COURSES, WEEK)

    for m in warm + cold:
        _persist_match(m, WEEK)
    return {"warm": len(warm), "open": len(cold),
            "placed": sum(3 for _ in warm + cold)}


def try_match_now(me: str) -> dict:
    """Late join: match the waiting pool among themselves, same day rule."""
    existing = STORE.thread_for(me, WEEK)
    if existing:
        STORE.leave_pool(me)
        return {"matched": True, "thread": existing}

    STORE.join_pool(me)
    users, _ = STORE.load_world()
    pool_ids = STORE.pool_members()
    pool = {z: users[z] for z in pool_ids if z in users}

    for m in run_open_matching(pool, _COURSES, WEEK):
        if me in m.member_zids():
            _persist_match(m, WEEK)
            for z in m.member_zids():
                STORE.leave_pool(z)
            return {"matched": True, "thread": STORE.thread_for(me, WEEK)}

    return {"matched": False, "waiting": len(STORE.pool_members())}


# --------------------------------------------------------------------------- #
# State the UI renders from (no hardcoded data anywhere on the client)
# --------------------------------------------------------------------------- #

def state_for(zid: str) -> dict:
    u = STORE.get_user(zid)
    friends = set(STORE.friends(zid))
    incoming = STORE.incoming_requests(zid)
    outgoing = set(STORE.outgoing_requests(zid))
    inc_ids = {r["zid"] for r in incoming}

    people = []
    for o in STORE.all_users():
        if o["zid"] == zid or o["zid"] in friends:
            continue
        people.append({
            "zid": o["zid"], "name": o["name"],
            "status": ("outgoing" if o["zid"] in outgoing
                       else "incoming" if o["zid"] in inc_ids else "none"),
        })

    thread = STORE.thread_for(zid, WEEK)
    return {
        "user": {
            "zid": u["zid"], "name": u["name"],
            "optedOut": bool(u["opted_out"]),
            "hasTimetable": STORE.has_timetable(zid),
            "friendCount": len(friends),
        },
        "friends": [
            {"zid": f, "name": (STORE.get_user(f) or {}).get("name", f)}
            for f in sorted(friends)
        ],
        "incoming": incoming,
        "outgoing": sorted(outgoing),
        "people": people,
        "sundayRun": STORE.thread_exists_for_week(WEEK),
        "thread": thread,
        "waiting": zid in STORE.pool_members(),
        "poolSize": len(STORE.pool_members()),
        "week": WEEK.isoformat(),
        "coursesApi": _MODE,
    }


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "CampusThread/1.0"

    # -- low-level helpers ------------------------------------------------- #

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode() or "{}")
        except json.JSONDecodeError:
            raise ApiError(400, "Malformed JSON body.")

    def _send(self, code, body: bytes, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200, extra=None):
        self._send(code, json.dumps(obj).encode(), "application/json", extra)

    def _session_zid(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = cookies.SimpleCookie(raw)
        if COOKIE not in jar:
            return None
        return STORE.session_user(jar[COOKIE].value)

    def _require(self) -> str:
        zid = self._session_zid()
        if not zid:
            raise ApiError(401, "Please log in.")
        return zid

    # -- routing ----------------------------------------------------------- #

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method: str):
        path = self.path.split("?", 1)[0]
        try:
            if method == "GET" and path in ("/", "/index.html"):
                return self._send(
                    200, UI_FILE.read_bytes(), "text/html; charset=utf-8"
                )
            if path == "/api/state":
                zid = self._session_zid()
                return self._json(
                    {"user": None} if not zid else state_for(zid)
                )
            handler = _ROUTES.get((method, path))
            if not handler:
                raise ApiError(404, "No such endpoint.")
            return handler(self)
        except ApiError as e:
            self._json({"error": e.message}, e.status)
        except ValueError as e:                       # store-level validation
            self._json({"error": str(e)}, 400)
        except Exception:                             # never leak a traceback
            traceback.print_exc()
            self._json({"error": "Something went wrong on our side."}, 500)

    def log_message(self, *_):
        pass


# -- endpoint implementations ---------------------------------------------- #

def _ep_register(h: Handler):
    d = h._body()
    STORE.register(d.get("zid", ""), d.get("name", ""), d.get("password", ""))
    zid = (d.get("zid") or "").strip().lower()
    if d.get("courses"):
        STORE.set_courses(zid, _split_codes(d["courses"]))
    token = STORE.open_session(zid)
    h._json(state_for(zid), 201, _cookie(token))


def _ep_login(h: Handler):
    d = h._body()
    zid = (d.get("zid") or "").strip().lower()
    if not STORE.verify(zid, d.get("password", "")):
        raise ApiError(401, "Wrong zID or password.")
    token = STORE.open_session(zid)
    h._json(state_for(zid), 200, _cookie(token))


def _ep_logout(h: Handler):
    raw = h.headers.get("Cookie")
    if raw:
        jar = cookies.SimpleCookie(raw)
        if COOKIE in jar:
            STORE.close_session(jar[COOKIE].value)
    h._json({"ok": True}, 200,
            [("Set-Cookie", f"{COOKIE}=; Path=/; Max-Age=0; SameSite=Lax")])


def _ep_timetable(h: Handler):
    zid = h._require()
    from .ics_parser import parse_ics
    text = h._body().get("ics", "")
    if "BEGIN:VEVENT" not in (text or ""):
        raise ApiError(422, "That doesn't look like a .ics calendar export.")
    n = STORE.set_timetable(zid, parse_ics(text))
    if n == 0:
        raise ApiError(422, "No class events found in that file.")
    h._json({"events": n, **state_for(zid)})


def _ep_courses(h: Handler):
    zid = h._require()
    STORE.set_courses(zid, _split_codes(h._body().get("courses", "")))
    h._json(state_for(zid))


def _ep_friend_request(h: Handler):
    zid = h._require()
    STORE.send_request(zid, (h._body().get("zid") or "").strip().lower())
    h._json(state_for(zid))


def _ep_friend_respond(h: Handler):
    zid = h._require()
    d = h._body()
    STORE.respond_request(
        zid, (d.get("zid") or "").strip().lower(), bool(d.get("accept"))
    )
    h._json(state_for(zid))


def _ep_opt_out(h: Handler):
    zid = h._require()
    STORE.set_opt_out(zid, bool(h._body().get("optedOut")))
    h._json(state_for(zid))


def _ep_run_sunday(h: Handler):
    zid = h._require()
    summary = run_sunday()
    h._json({"ran": summary, **state_for(zid)})


def _ep_join_now(h: Handler):
    zid = h._require()
    result = try_match_now(zid)
    h._json({"join": result, **state_for(zid)})


def _ep_message(h: Handler):
    zid = h._require()
    thread = STORE.thread_for(zid, WEEK)
    if not thread:
        raise ApiError(404, "You have no thread this week.")
    STORE.post_message(thread["id"], zid, h._body().get("body", ""))
    h._json(state_for(zid))


def _ep_we_met(h: Handler):
    zid = h._require()
    thread = STORE.thread_for(zid, WEEK)
    if not thread:
        raise ApiError(404, "You have no thread this week.")
    res = STORE.complete_thread(
        thread["id"], zid, bool(h._body().get("friend"))
    )
    names = {m["zid"]: m["name"] for m in thread["members"]}
    res["friendedNames"] = [
        sorted(names.get(x, x) for x in pair) for pair in res["friended"]
    ]
    h._json({"weMet": res, **state_for(zid)})


def _split_codes(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(c) for c in raw]
    return [c for c in str(raw).replace(",", " ").split()]


def _cookie(token: str) -> list[tuple[str, str]]:
    return [("Set-Cookie",
             f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax")]


_ROUTES = {
    ("POST", "/api/register"): _ep_register,
    ("POST", "/api/login"): _ep_login,
    ("POST", "/api/logout"): _ep_logout,
    ("POST", "/api/timetable"): _ep_timetable,
    ("POST", "/api/courses"): _ep_courses,
    ("POST", "/api/friends/request"): _ep_friend_request,
    ("POST", "/api/friends/respond"): _ep_friend_respond,
    ("POST", "/api/opt-out"): _ep_opt_out,
    ("POST", "/api/run-sunday"): _ep_run_sunday,
    ("POST", "/api/join-now"): _ep_join_now,
    ("POST", "/api/thread/message"): _ep_message,
    ("POST", "/api/we-met"): _ep_we_met,
}


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    port = int(argv[0]) if argv else 8000
    if seed(STORE):
        print("Seeded demo data (all passwords: 'password').")
        print("  Warm cluster : z1 Jamie · z2 Alex · z3 Sam · z4 Dev")
        print("  Friendless   : z5 Mei · z6 Omar · z7 Lina  (no friends)")
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"\nCampusThread live at {url}   ·   DevSoc API: {_MODE}")
    print("Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
        srv.server_close()


if __name__ == "__main__":
    main()
