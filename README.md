# CampusThread — Matching Algorithm

Hackathon deliverable #2 (the matcher) + #1 (`.ics` parsing), now wired to
the **real DevSoc Hasuragres GraphQL API**.

## Run it

```bash
python -m campusthread.server   # the full web app  →  http://127.0.0.1:8000
python -m campusthread.demo     # CLI: matching + rooms + the We-met loop
python -m campusthread.tests    # 21 invariant + integration tests
```

### The web app (`campusthread.server`)

A real multi-user application — stdlib only (`http.server` + `sqlite3`),
no Flask, no Docker. On first start it seeds demo accounts (all passwords
`password`) and prints them:

* **Warm cluster** — `z1` Jamie, `z2` Alex, `z3` Sam, `z4` Dev (timetables +
  friendships from the original story).
* **Friendless** — `z5` Mei, `z6` Omar, `z7` Lina: no social graph at all
  (e.g. just-arrived international students).

Log in, **Run Sunday matching**, and open the group chat. Everything the
browser shows comes from the API — there is no hardcoded data on the client.
What's implemented end-to-end:

| Feature | Where |
|---|---|
| Register / login / logout, hashed passwords, server sessions | `store.py`, `server.py` |
| Upload your real timetable (`.ics`) — parsed & stored | `ics_parser.py` → SQLite |
| Friend **requests** (send / accept / decline); reciprocal auto-accept | `store.py` |
| Sunday 7 pm run — warm intros **+** a cold-start pass; idempotent | `matching.run_weekly_matching` / `run_open_matching` |
| **Friendless / missed-the-window** users get their own homepage and a "Find me a group now" pool — matched only with others in the same situation, same shared-on-campus-day rule | `run_open_matching`, `/api/join-now` |
| Live group chat with persisted messages | `messages` table |
| "We met" → marks the pair, optionally friends them, mutating the graph | `store.complete_thread` |

Persistence is a real SQLite database (`campusthread/campusthread.db`):
every write is one locked, atomic transaction, and the weekly job is
idempotent via a `UNIQUE` constraint — the durability gaps the JSON
`FeedbackStore` had. The matcher is **unchanged**: `DbFeedback` adapts the
database to the exact interface `run_weekly_matching` expects (the same
Protocol-seam idea as the courses client).

Delete `campusthread/campusthread.db` to reset to a fresh seeded world.

## The real UNSW API (important — read this)

The brief assumed a REST "UNSW Courses API" with a `faculty` field. The
actual DevSoc API is **GraphQL**, at:

```
https://graphql.csesoc.app/v1/graphql      (public, no auth for reads)
```

Schema verified against DevSoc's own production code (Notangles + Freerooms),
not guessed. Brief → reality mapping:

| Brief assumed | Reality (Hasuragres) |
|---|---|
| REST endpoints | One GraphQL endpoint, Hasura-generated |
| `Courses.faculty`, `Courses.school` | **Do not exist.** `courses` has only `course_code`, `course_name` (README: course info "COMING SOON") |
| `Classes`, `Times` | Nested: `courses { classes { ... times { day time weeks location } } }`, filtered by `term`/`year` |
| `Buildings(id,name,lat,long)` | exists, plus `aliases` |
| `Rooms(id,name,usage,capacity)` | exists, plus `abbr`, `school` (school that *administers the room* — not a course faculty) |
| `Bookings(name,bookingType,start,end)` | `rooms { bookings(where:{start,end}) { name bookingType start end } }` |

### The faculty gap and how it's handled

The brief's icebreaker/ranking tiebreak needs a shared *faculty*. The API
doesn't provide one, so `hasuragres.py` derives it from the 4-letter
course-code prefix via a curated map (`COMP -> Engineering`,
`FINS -> Business`, ...; unknown prefixes fall back to the prefix itself).
"You're both taking COMP courses" is always true and is a perfectly good
icebreaker — a documented proxy, not invented data. When DevSoc ships real
course info, only `HasuragresCoursesClient.get_course` changes.

## Why the matcher didn't change

`matching.py` depends on the `UnswCoursesClient` *protocol*, not any
implementation. `HasuragresCoursesClient` satisfies that protocol, so the
algorithm, ranking and tests are byte-for-byte unchanged. Swapping the stub
for the live API was a one-file addition.
(`test_matcher_unchanged_with_real_client` proves this.)

## Offline behaviour

This sandbox can't reach `graphql.csesoc.app`, so the demo prints
`Courses API: OFFLINE` and the client degrades gracefully — the faculty
proxy still works, only live course *names* fall back to the code. **Run the
demo on a machine with internet to see it hit the live API** (it prints
`LIVE` and real course names). The GraphQL transport is injectable, so all
integration tests run fully offline against a mock transport.

## Layout

| File | Responsibility |
|------|----------------|
| `models.py`        | Value objects: `User`, `Timetable`, `ClassEvent`, `Match`, ... |
| `ics_parser.py`    | Zero-dependency `.ics` parser (unfolding, TZID, weekly RRULE) |
| `social_graph.py`  | Friendships -> candidate triplets |
| `unsw_courses.py`  | `UnswCoursesClient` protocol + `StaticCourses` (offline/tests) |
| `hasuragres.py`    | **Real GraphQL client** + courses-protocol impl + faculty proxy |
| `matching.py`      | **The algorithm** + meetup-window maths + We-met signals |
| `room_resolver.py` | Meetup window → a free, nearby, right-sized room |
| `feedback.py`      | **The We-met loop**: logging, JSON persistence, graph mutation |
| `icebreaker.py`    | Auto-generated intro from a `Match` |
| `ui/groupchat.html`| Single-file interactive demo UI (notify → chat → we-met) |
| `demo.py` / `tests.py` | End-to-end run / invariant + integration tests |

## The We-met feedback loop (closes the core loop)

`feedback.py` makes "We met" actually feed back, exactly as the brief
describes:

* **Friend-add mutates the graph.** A new B–C edge means they're no longer a
  warm intro, *and* it grows everyone's second-degree reach — new
  introductions become possible.
* **A met pair is never re-introduced.** The product did its job for them.
* **A pair introduced but not met is rested** for a cooldown (absence of
  "we met" is itself signal — don't spam the same non-connection).
* **Proven matchmakers get a bounded nudge.** A connector whose past intros
  led to real meetups ranks slightly higher — always *below* the brief's
  day/faculty ordering, so it only ever breaks ties.

Outcomes persist to JSON (the demo's stand-in for the brief's PostgreSQL),
so the loop survives across Sunday runs. It's all local — `matching.py`
stays network-free, and `feedback=None` is byte-for-byte the old behaviour
(there's a test that guarantees this).

The demo shows it concretely: Jamie introduces Alex & Sam → they meet and
add each other → re-running the same Sunday, that thread can't recur, but
because they connected **Alex can now introduce Sam to Dev — a person Sam
had no path to before**, and Jamie is credited as a matchmaker. That last
line is the pitch's success metric.

## Room resolution

`room_resolver.py` turns each `MeetupWindow` into an actual space using the
verified Freerooms schema. It resolves the trio's class `LOCATION` strings to
buildings, takes the centroid as an anchor, then finds rooms with **no
booking overlapping the whole window**, ranked nearest-first and
smallest-that-fits (a 12-seat meeting room beats a 350-seat theatre for
three people). It's deliberately separate from `matching.py` so the matcher
stays network-free; `enrich_match_with_rooms(match, client)` is the hook the
chat-creation step calls. Demo output, against the offline fixture:

```
Wed 11:00–12:00  →  K17 G01 (K17 CSE Building, seats 12) · ~36m away
```

## The UI

`ui/groupchat.html` is a self-contained, dependency-free demo (open it in
any browser — no build). Three tappable screens map to the brief:

1. **The thread opened** — Sunday notification; the warm-intro structure is
   drawn as a literal stitched thread between the three people.
2. **The groupchat** — the auto-generated icebreaker as the first message, a
   live countdown to Friday midnight (the soft deadline), and the 2–3
   meetup windows as tappable tickets, each carrying its resolved room.
   "We met" is always visible.
3. **We met** — confirmation + optional friend-add that "feeds back into
   future match quality", closing on the brief's success metric.

The window/room data in the UI is the exact pipeline output (Jamie ▸ Alex ▸
Sam, K17 G01). Aesthetic is intentional — warm paper/ink, editorial serif,
ember accent — not a generic template.
