"""Result shaping for multi-room group / all-rooms command forwarding.

Kept as a dependency-free module so the outcome of an "everywhere" command can
be asserted directly, without booting the intent pipeline.

The rule this module exists to hold: a forward that reached **no** room is a
failure the user must hear about. Reporting ``success`` for an empty room list
is the false-success shape that made "play everywhere" answer
``Sent play to all rooms (0 rooms)`` while nothing played anywhere (#4432) --
the same class as reporting a completed order on an empty cart.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "NO_ROOMS_PAIRED_ERROR",
    "build_group_forward_result",
]

NO_ROOMS_PAIRED_ERROR = "no_rooms_paired"
_FORWARD_FAILED_ERROR = "group_forward_failed"


def build_group_forward_result(
    *,
    intent: str,
    display_name: str,
    target_room: str | None,
    room_ids: list[str],
    delivered_room_ids: list[str],
) -> dict[str, Any]:
    """Build the command result for a group / all-rooms forward.

    Args:
        intent: The forwarded intent (``play``, ``pause``, ...).
        display_name: Human-readable group name used in the spoken reply.
        target_room: The room phrase the user actually said, if any.
        room_ids: Every room the command was meant to reach.
        delivered_room_ids: The subset the forwarder actually reached.

    Returns:
        A command result that reports success only when at least one room was
        reached, and otherwise says plainly what went wrong.
    """
    if not room_ids:
        return {
            "success": False,
            "message": (
                "No speakers are paired yet, so there was nowhere to send %s. "
                "Pair a speaker first, then try again." % intent
            ),
            "data": {
                "target_group": display_name,
                "target_room": target_room,
                "room_count": 0,
                "delivered_room_count": 0,
            },
            "error": NO_ROOMS_PAIRED_ERROR,
        }

    delivered_count = len(delivered_room_ids)
    if delivered_count == 0:
        return {
            "success": False,
            "message": "Couldn't reach any of the %d speakers in %s." % (len(room_ids), display_name),
            "data": {
                "target_group": display_name,
                "target_room": target_room,
                "room_count": len(room_ids),
                "delivered_room_count": 0,
            },
            "error": _FORWARD_FAILED_ERROR,
        }

    return {
        "success": True,
        "message": "Sent %s to %s (%d rooms)" % (intent, display_name, delivered_count),
        "data": {
            "target_group": display_name,
            "room_count": delivered_count,
            "requested_room_count": len(room_ids),
            "delivered_room_count": delivered_count,
        },
        "error": None,
    }
