"""
Run CampusThread as a localhost website.

    python -m campusthread.server          # http://127.0.0.1:8000
    python -m campusthread.server 9000     # custom port

Zero dependencies — stdlib `http.server` only, same ethos as the rest of the
project (no Flask, no Docker needed). Two routes:

    GET /              -> the groupchat UI (campusthread/ui/groupchat.html)
    GET /api/thread    -> the *live* pipeline output as JSON: the headline
                          thread for the demo week, its meetup windows and
                          resolved rooms, and the generated icebreaker.

The UI fetches /api/thread and renders real matcher output. Opened as a
bare file:// it falls back to its baked-in sample, so the static demo still
works offline. Tries the live DevSoc GraphQL API first, then the offline
fixture — exactly like demo.py.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .demo import WEEK_START, _build_client, _load
from .icebreaker import generate_icebreaker
from .matching import run_weekly_matching
from .room_resolver import enrich_match_with_rooms

UI_FILE = Path(__file__).parent / "ui" / "groupchat.html"


def build_thread_payload() -> dict:
    """Run the real Sunday pipeline and shape it for the UI."""
    client, mode = _build_client()
    users, graph, courses = _load(client)
    matches = run_weekly_matching(users, graph, courses, WEEK_START)
    if not matches:
        return {"mode": mode, "matches": 0, "windows": []}

    m = matches[0]
    enrich_match_with_rooms(m, client)

    windows = []
    for w, room in zip(m.meetup_windows, m.room_suggestions):
        windows.append(
            {
                "when": w.label(),
                "room": room.room_name if room else "—",
                "bldg": room.building_name if room else "no free room",
                "seats": room.capacity if room else 0,
                "dist": round(room.distance_m)
                if room and room.distance_m is not None
                else "?",
            }
        )

    return {
        "mode": mode,
        "matches": len(matches),
        "connector": m.connector.name,
        "members": [m.member_b.name, m.member_c.name],
        "faculty": m.shared_faculty,
        "icebreaker": generate_icebreaker(m),
        "windows": windows,
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib-mandated name)
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/thread":
            try:
                body = json.dumps(build_thread_payload()).encode()
                self._send(200, body, "application/json")
            except Exception as exc:  # never 500 the demo; report it as JSON
                self._send(
                    200,
                    json.dumps({"error": str(exc), "windows": []}).encode(),
                    "application/json",
                )
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *_args) -> None:  # quiet: no per-request stderr spam
        pass


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    port = int(argv[0]) if argv else 8000
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"CampusThread is live at {url}  (Ctrl+C to stop)")
    print(f"  UI        {url}/")
    print(f"  Live API  {url}/api/thread")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
        server.server_close()


if __name__ == "__main__":
    main()
