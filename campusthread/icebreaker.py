"""
The auto-generated first message of a thread.

A warm intro lands better than a cold one: name the mutual friend, name the
thing they have in common, and hand them a concrete next step (a time and,
if the room resolver has run, a place). No template that reads like a form —
it should sound like the connector actually said it.
"""

from __future__ import annotations

from .models import Match


def generate_icebreaker(match: Match) -> str:
    names = [m.name for m in match.members()]

    common = (
        f"you're all in {match.shared_faculty}"
        if match.shared_faculty
        else "different corners of campus"
    )

    when = ""
    if match.meetup_windows:
        w = match.meetup_windows[0]
        room = match.room_suggestions[0] if match.room_suggestions else None
        where = f" at {room.room_name} ({room.building_name})" if room else ""
        when = f" You're all free {w.label()}{where} — grab a coffee?"

    if match.kind == "open":
        # No mutual friend — a cold-start group (friendless / missed the
        # window). Don't fake a connector; be honest and warm about it.
        trio = ", ".join(names[:-1]) + f" & {names[-1]}"
        return (
            f"{trio} — none of you share a mutual friend yet, but you're all "
            f"on campus the same day ({common}).{when} "
            f"Everyone starts somewhere."
        )

    a = match.connector.name
    b, c = match.member_b.name, match.member_c.name
    pair_common = (
        f"you're both in {match.shared_faculty}"
        if match.shared_faculty
        else "you've somehow never crossed paths"
    )
    return (
        f"{b}, meet {c} — {c}, meet {b}. {a} reckons you two should know "
        f"each other ({pair_common}).{when}"
    )
