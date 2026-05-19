"""
Persistence — a real, zero-setup SQLite database.

This replaces the JSON `FeedbackStore` stand-in with an actual relational
store: users, password credentials, sessions, a friendship edge table,
friend requests, timetable events, the formed threads + their messages, and
the append-only introductions log the feedback loop reads.

Why SQLite: it needs no server to install (stdlib `sqlite3`), yet gives
real transactions and constraints. Every write goes through one short
transaction under a process lock, so a crash mid-write can't corrupt state
and re-running the Sunday job can't double-insert (the `introductions`
UNIQUE constraint enforces idempotency) — the two durability gaps the
earlier review flagged in the JSON store.

`DbFeedback` adapts this store to the exact interface `matching.py` expects
(`met_pairs` / `rested_pairs` / `connector_success`), so the matcher runs
unchanged against the database — the same Protocol seam used for courses.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
from contextlib import closing, contextmanager
from datetime import date, datetime
from pathlib import Path

from .models import ClassEvent, Timetable, User
from .social_graph import SocialGraph

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    zid        TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    pw_salt    TEXT NOT NULL,
    pw_hash    TEXT NOT NULL,
    opted_out  INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    zid        TEXT NOT NULL REFERENCES users(zid),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS enrollments (
    zid         TEXT NOT NULL REFERENCES users(zid),
    course_code TEXT NOT NULL,
    PRIMARY KEY (zid, course_code)
);
CREATE TABLE IF NOT EXISTS friendships (
    a TEXT NOT NULL,
    b TEXT NOT NULL,
    PRIMARY KEY (a, b),
    CHECK (a < b)
);
CREATE TABLE IF NOT EXISTS friend_requests (
    from_zid   TEXT NOT NULL REFERENCES users(zid),
    to_zid     TEXT NOT NULL REFERENCES users(zid),
    status     TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    PRIMARY KEY (from_zid, to_zid)
);
CREATE TABLE IF NOT EXISTS timetable_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    zid      TEXT NOT NULL REFERENCES users(zid),
    summary  TEXT NOT NULL,
    location TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS threads (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    week       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    faculty    TEXT,
    icebreaker TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS thread_members (
    thread_id INTEGER NOT NULL REFERENCES threads(id),
    zid       TEXT NOT NULL REFERENCES users(zid),
    role      TEXT NOT NULL,
    PRIMARY KEY (thread_id, zid)
);
CREATE TABLE IF NOT EXISTS thread_windows (
    thread_id INTEGER NOT NULL REFERENCES threads(id),
    ord       INTEGER NOT NULL,
    label     TEXT NOT NULL,
    room      TEXT, bldg TEXT, seats INTEGER, dist TEXT,
    PRIMARY KEY (thread_id, ord)
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id  INTEGER NOT NULL REFERENCES threads(id),
    zid        TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS introductions (
    connector TEXT NOT NULL,
    b         TEXT NOT NULL,
    c         TEXT NOT NULL,
    week      TEXT NOT NULL,
    met       INTEGER NOT NULL DEFAULT 0,
    kind      TEXT NOT NULL DEFAULT 'warm-intro',
    PRIMARY KEY (connector, b, c, week)
);
CREATE TABLE IF NOT EXISTS waiting_pool (
    zid       TEXT PRIMARY KEY REFERENCES users(zid),
    joined_at TEXT NOT NULL
);
"""


def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def _hash_pw(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), 120_000
    ).hex()


class Store:
    """All persistence. Every mutating call is one locked transaction."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.Lock()
        with self._tx() as cx:
            cx.executescript(_SCHEMA)

    @contextmanager
    def _tx(self):
        """One short, serialized, atomic transaction."""
        with self._lock, closing(sqlite3.connect(self.path)) as cx:
            cx.execute("PRAGMA foreign_keys = ON")
            cx.row_factory = sqlite3.Row
            try:
                yield cx
                cx.commit()
            except Exception:
                cx.rollback()
                raise

    # -- auth ------------------------------------------------------------- #

    def register(self, zid: str, name: str, password: str) -> None:
        zid, name = zid.strip().lower(), name.strip()
        if not zid or not name or len(password) < 6:
            raise ValueError("zID, name and a 6+ char password are required.")
        salt = secrets.token_hex(16)
        with self._tx() as cx:
            if cx.execute("SELECT 1 FROM users WHERE zid=?", (zid,)).fetchone():
                raise ValueError(f"{zid} is already registered — try logging in.")
            cx.execute(
                "INSERT INTO users(zid,name,pw_salt,pw_hash,created_at) "
                "VALUES (?,?,?,?,?)",
                (zid, name, salt, _hash_pw(password, salt), _now()),
            )

    def verify(self, zid: str, password: str) -> bool:
        zid = zid.strip().lower()
        with self._tx() as cx:
            row = cx.execute(
                "SELECT pw_salt,pw_hash FROM users WHERE zid=?", (zid,)
            ).fetchone()
        return bool(
            row and secrets.compare_digest(row["pw_hash"], _hash_pw(password, row["pw_salt"]))
        )

    def open_session(self, zid: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._tx() as cx:
            cx.execute(
                "INSERT INTO sessions(token,zid,created_at) VALUES (?,?,?)",
                (token, zid.strip().lower(), _now()),
            )
        return token

    def session_user(self, token: str | None) -> str | None:
        if not token:
            return None
        with self._tx() as cx:
            row = cx.execute(
                "SELECT zid FROM sessions WHERE token=?", (token,)
            ).fetchone()
        return row["zid"] if row else None

    def close_session(self, token: str) -> None:
        with self._tx() as cx:
            cx.execute("DELETE FROM sessions WHERE token=?", (token,))

    # -- profile / people ------------------------------------------------- #

    def get_user(self, zid: str) -> dict | None:
        with self._tx() as cx:
            row = cx.execute(
                "SELECT zid,name,opted_out FROM users WHERE zid=?", (zid,)
            ).fetchone()
        return dict(row) if row else None

    def set_opt_out(self, zid: str, value: bool) -> None:
        with self._tx() as cx:
            cx.execute(
                "UPDATE users SET opted_out=? WHERE zid=?", (1 if value else 0, zid)
            )

    def set_courses(self, zid: str, codes: list[str]) -> None:
        with self._tx() as cx:
            cx.execute("DELETE FROM enrollments WHERE zid=?", (zid,))
            cx.executemany(
                "INSERT OR IGNORE INTO enrollments(zid,course_code) VALUES (?,?)",
                [(zid, c.strip().upper()) for c in codes if c.strip()],
            )

    def all_users(self) -> list[dict]:
        with self._tx() as cx:
            return [dict(r) for r in cx.execute(
                "SELECT zid,name FROM users ORDER BY name"
            ).fetchall()]

    # -- friendships ------------------------------------------------------ #

    def friends(self, zid: str) -> list[str]:
        with self._tx() as cx:
            rows = cx.execute(
                "SELECT a,b FROM friendships WHERE a=? OR b=?", (zid, zid)
            ).fetchall()
        return [r["b"] if r["a"] == zid else r["a"] for r in rows]

    def send_request(self, frm: str, to: str) -> None:
        if frm == to:
            raise ValueError("You can't friend yourself.")
        with self._tx() as cx:
            if not cx.execute("SELECT 1 FROM users WHERE zid=?", (to,)).fetchone():
                raise ValueError(f"No user {to}.")
            a, b = _pair(frm, to)
            if cx.execute(
                "SELECT 1 FROM friendships WHERE a=? AND b=?", (a, b)
            ).fetchone():
                raise ValueError("You're already friends.")
            # An incoming request from the same person -> accept it instead.
            if cx.execute(
                "SELECT 1 FROM friend_requests WHERE from_zid=? AND to_zid=? "
                "AND status='pending'",
                (to, frm),
            ).fetchone():
                self._accept(cx, to, frm)
                return
            cx.execute(
                "INSERT OR REPLACE INTO friend_requests"
                "(from_zid,to_zid,status,created_at) VALUES (?,?, 'pending', ?)",
                (frm, to, _now()),
            )

    def respond_request(self, me: str, frm: str, accept: bool) -> None:
        with self._tx() as cx:
            req = cx.execute(
                "SELECT 1 FROM friend_requests WHERE from_zid=? AND to_zid=? "
                "AND status='pending'",
                (frm, me),
            ).fetchone()
            if not req:
                raise ValueError("No such pending request.")
            if accept:
                self._accept(cx, frm, me)
            else:
                cx.execute(
                    "UPDATE friend_requests SET status='declined' "
                    "WHERE from_zid=? AND to_zid=?",
                    (frm, me),
                )

    def _accept(self, cx, frm: str, to: str) -> None:
        a, b = _pair(frm, to)
        cx.execute("INSERT OR IGNORE INTO friendships(a,b) VALUES (?,?)", (a, b))
        cx.execute(
            "UPDATE friend_requests SET status='accepted' "
            "WHERE (from_zid=? AND to_zid=?) OR (from_zid=? AND to_zid=?)",
            (frm, to, to, frm),
        )

    def seed_friendship(self, a: str, b: str) -> None:
        """Direct edge insert — used only by demo seeding, not the API."""
        x, y = _pair(a, b)
        with self._tx() as cx:
            cx.execute("INSERT OR IGNORE INTO friendships(a,b) VALUES (?,?)", (x, y))

    def incoming_requests(self, zid: str) -> list[dict]:
        with self._tx() as cx:
            return [dict(r) for r in cx.execute(
                "SELECT r.from_zid AS zid, u.name FROM friend_requests r "
                "JOIN users u ON u.zid=r.from_zid "
                "WHERE r.to_zid=? AND r.status='pending'",
                (zid,),
            ).fetchall()]

    def outgoing_requests(self, zid: str) -> list[str]:
        with self._tx() as cx:
            return [r["to_zid"] for r in cx.execute(
                "SELECT to_zid FROM friend_requests "
                "WHERE from_zid=? AND status='pending'",
                (zid,),
            ).fetchall()]

    # -- timetable -------------------------------------------------------- #

    def set_timetable(self, zid: str, tt: Timetable) -> int:
        with self._tx() as cx:
            cx.execute("DELETE FROM timetable_events WHERE zid=?", (zid,))
            cx.executemany(
                "INSERT INTO timetable_events"
                "(zid,summary,location,starts_at,ends_at) VALUES (?,?,?,?,?)",
                [
                    (zid, e.summary, e.location,
                     e.start.isoformat(), e.end.isoformat())
                    for e in tt.events
                ],
            )
        return len(tt.events)

    def has_timetable(self, zid: str) -> bool:
        with self._tx() as cx:
            return bool(cx.execute(
                "SELECT 1 FROM timetable_events WHERE zid=? LIMIT 1", (zid,)
            ).fetchone())

    # -- world snapshot the matcher consumes ------------------------------ #

    def load_world(self) -> tuple[dict[str, User], SocialGraph]:
        """Reconstruct the in-memory `User` map + `SocialGraph`."""
        with self._tx() as cx:
            urows = cx.execute(
                "SELECT zid,name,opted_out FROM users"
            ).fetchall()
            erows = cx.execute("SELECT zid,course_code FROM enrollments").fetchall()
            trows = cx.execute(
                "SELECT zid,summary,location,starts_at,ends_at FROM timetable_events"
            ).fetchall()
            frows = cx.execute("SELECT a,b FROM friendships").fetchall()

        courses: dict[str, list[str]] = {}
        for r in erows:
            courses.setdefault(r["zid"], []).append(r["course_code"])
        events: dict[str, list[ClassEvent]] = {}
        for r in trows:
            events.setdefault(r["zid"], []).append(
                ClassEvent(
                    r["summary"], r["location"],
                    datetime.fromisoformat(r["starts_at"]),
                    datetime.fromisoformat(r["ends_at"]),
                )
            )
        users = {
            r["zid"]: User(
                r["zid"], r["name"], courses.get(r["zid"], []),
                Timetable(events.get(r["zid"], [])),
                bool(r["opted_out"]),
            )
            for r in urows
        }
        graph = SocialGraph()
        for zid in users:
            graph.add_user(zid)
        for r in frows:
            graph.add_friendship(r["a"], r["b"])
        return users, graph

    # -- threads ---------------------------------------------------------- #

    def thread_exists_for_week(self, week: date) -> bool:
        with self._tx() as cx:
            return bool(cx.execute(
                "SELECT 1 FROM threads WHERE week=? LIMIT 1", (week.isoformat(),)
            ).fetchone())

    def clear_week(self, week: date) -> None:
        """Make the Sunday run idempotent: drop this week's threads first."""
        w = week.isoformat()
        with self._tx() as cx:
            ids = [r["id"] for r in cx.execute(
                "SELECT id FROM threads WHERE week=?", (w,)
            ).fetchall()]
            for tid in ids:
                cx.execute("DELETE FROM messages WHERE thread_id=?", (tid,))
                cx.execute("DELETE FROM thread_members WHERE thread_id=?", (tid,))
                cx.execute("DELETE FROM thread_windows WHERE thread_id=?", (tid,))
            cx.execute("DELETE FROM threads WHERE week=?", (w,))
            # Drop this week's *unmet* introductions too: re-running the job
            # regenerates them, so keeping them would let the just-recorded
            # rows rest their own pairs and block regeneration. Met intros
            # (the real feedback signal) and prior weeks are untouched.
            cx.execute(
                "DELETE FROM introductions WHERE week=? AND met=0", (w,)
            )

    def save_thread(
        self, week: date, kind: str, faculty: str | None,
        icebreaker: str, members: list[tuple[str, str]],
        windows: list[dict],
    ) -> int:
        with self._tx() as cx:
            cur = cx.execute(
                "INSERT INTO threads(week,kind,faculty,icebreaker,created_at) "
                "VALUES (?,?,?,?,?)",
                (week.isoformat(), kind, faculty, icebreaker, _now()),
            )
            tid = cur.lastrowid
            cx.executemany(
                "INSERT INTO thread_members(thread_id,zid,role) VALUES (?,?,?)",
                [(tid, z, role) for z, role in members],
            )
            cx.executemany(
                "INSERT INTO thread_windows"
                "(thread_id,ord,label,room,bldg,seats,dist) VALUES (?,?,?,?,?,?,?)",
                [
                    (tid, i, w["when"], w["room"], w["bldg"], w["seats"], str(w["dist"]))
                    for i, w in enumerate(windows)
                ],
            )
            return tid

    def thread_for(self, zid: str, week: date) -> dict | None:
        with self._tx() as cx:
            row = cx.execute(
                "SELECT t.* FROM threads t JOIN thread_members m ON m.thread_id=t.id "
                "WHERE m.zid=? AND t.week=? ORDER BY t.id DESC LIMIT 1",
                (zid, week.isoformat()),
            ).fetchone()
            if not row:
                return None
            tid = row["id"]
            members = [dict(r) for r in cx.execute(
                "SELECT m.zid, m.role, u.name FROM thread_members m "
                "JOIN users u ON u.zid=m.zid WHERE m.thread_id=? ", (tid,)
            ).fetchall()]
            windows = [dict(r) for r in cx.execute(
                "SELECT label AS 'when', room, bldg, seats, dist "
                "FROM thread_windows WHERE thread_id=? ORDER BY ord", (tid,)
            ).fetchall()]
            msgs = [dict(r) for r in cx.execute(
                "SELECT m.zid, u.name, m.body, m.created_at FROM messages m "
                "JOIN users u ON u.zid=m.zid WHERE m.thread_id=? ORDER BY m.id",
                (tid,),
            ).fetchall()]
        return {
            "id": tid, "kind": row["kind"], "faculty": row["faculty"],
            "icebreaker": row["icebreaker"], "members": members,
            "windows": windows, "messages": msgs,
        }

    def post_message(self, thread_id: int, zid: str, body: str) -> None:
        body = body.strip()
        if not body:
            raise ValueError("Empty message.")
        with self._tx() as cx:
            if not cx.execute(
                "SELECT 1 FROM thread_members WHERE thread_id=? AND zid=?",
                (thread_id, zid),
            ).fetchone():
                raise ValueError("You are not in this thread.")
            cx.execute(
                "INSERT INTO messages(thread_id,zid,body,created_at) "
                "VALUES (?,?,?,?)",
                (thread_id, zid, body[:2000], _now()),
            )

    # -- waiting pool (missed the Sunday window) -------------------------- #

    def join_pool(self, zid: str) -> None:
        with self._tx() as cx:
            cx.execute(
                "INSERT OR REPLACE INTO waiting_pool(zid,joined_at) VALUES (?,?)",
                (zid, _now()),
            )

    def leave_pool(self, zid: str) -> None:
        with self._tx() as cx:
            cx.execute("DELETE FROM waiting_pool WHERE zid=?", (zid,))

    def pool_members(self) -> list[str]:
        with self._tx() as cx:
            return [r["zid"] for r in cx.execute(
                "SELECT zid FROM waiting_pool ORDER BY joined_at"
            ).fetchall()]

    # -- introductions / feedback ---------------------------------------- #

    def record_introductions(self, rows: list[tuple], week: date) -> None:
        """rows: (connector, b, c, kind). Idempotent via the PK."""
        with self._tx() as cx:
            cx.executemany(
                "INSERT OR IGNORE INTO introductions"
                "(connector,b,c,week,met,kind) VALUES (?,?,?,?,0,?)",
                [(con, b, c, week.isoformat(), kind) for con, b, c, kind in rows],
            )

    def mark_met(self, b: str, c: str, week: date, *, friended: bool) -> None:
        with self._tx() as cx:
            cx.execute(
                "UPDATE introductions SET met=1 "
                "WHERE week=? AND ((b=? AND c=?) OR (b=? AND c=?))",
                (week.isoformat(), b, c, c, b),
            )
            if friended:
                a, bb = _pair(b, c)
                cx.execute(
                    "INSERT OR IGNORE INTO friendships(a,b) VALUES (?,?)", (a, bb)
                )

    def complete_thread(self, thread_id: int, me: str, friended: bool) -> dict:
        """
        'We met' for a whole thread: every pair in it is marked met (so the
        loop never re-introduces them), and — if they chose to — friended,
        which mutates the graph and unlocks new second-degree intros.
        """
        from itertools import combinations

        with self._tx() as cx:
            row = cx.execute(
                "SELECT week FROM threads WHERE id=?", (thread_id,)
            ).fetchone()
            if row is None:
                raise ValueError("No such thread.")
            week = row["week"]
            mem = [r["zid"] for r in cx.execute(
                "SELECT zid FROM thread_members WHERE thread_id=?", (thread_id,)
            ).fetchall()]
            if me not in mem:
                raise ValueError("You are not in this thread.")
            edges: list[tuple[str, str]] = []
            for x, y in combinations(sorted(mem), 2):
                cx.execute(
                    "UPDATE introductions SET met=1 WHERE week=? AND "
                    "((b=? AND c=?) OR (b=? AND c=?))",
                    (week, x, y, y, x),
                )
                if friended and not cx.execute(
                    "SELECT 1 FROM friendships WHERE a=? AND b=?", (x, y)
                ).fetchone():
                    cx.execute(
                        "INSERT INTO friendships(a,b) VALUES (?,?)", (x, y)
                    )
                    edges.append((x, y))  # only genuinely new edges
        return {"week": week, "members": mem, "friended": edges}


class DbFeedback:
    """
    Adapts `Store` to the 3-method interface `matching.run_weekly_matching`
    expects. Same Protocol-seam idea as the courses client: the matcher is
    oblivious to whether feedback lives in JSON or SQLite.
    """

    def __init__(self, store: Store):
        self._s = store

    def _intros(self) -> list[sqlite3.Row]:
        with self._s._tx() as cx:
            return cx.execute(
                "SELECT connector,b,c,week,met FROM introductions"
            ).fetchall()

    def met_pairs(self) -> set[frozenset]:
        return {
            frozenset((r["b"], r["c"])) for r in self._intros() if r["met"]
        }

    def rested_pairs(self, current_week: date, cooldown_weeks: int) -> set[frozenset]:
        rested: set[frozenset] = set()
        for r in self._intros():
            if r["met"]:
                continue
            wk = date.fromisoformat(r["week"])
            weeks_ago = (current_week - wk).days / 7
            if 0 <= weeks_ago <= cooldown_weeks:
                rested.add(frozenset((r["b"], r["c"])))
        return rested

    def connector_success(self, zid: str) -> int:
        return sum(
            1 for r in self._intros() if r["connector"] == zid and r["met"]
        )


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
