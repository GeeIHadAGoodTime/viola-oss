#!/usr/bin/env python3
"""Strict check that Spotify, and nothing else, is what's playing.

"Sound played" proved nothing in build 9: the sound was YouTube. This passes only
when all of these hold at once, read from the running Viola:

* the player's provider is Spotify and the item has no YouTube video id;
* the browser reports a real Spotify track id;
* the position moved between two reads;
* the hidden browser's own Windows audio session is making sound;
* the diagnostic names no broken link;
* Viola's own player shows what the stage shows: a title, a cover, and a
  position that moves between two reads (build 10 on the laptop played
  Spotify while the player said nothing was playing).

    python spotify_strict_check.py [--port 8756] [--api-key-file PATH]

Exit code 0 = pass. Prints the first failure in plain words otherwise.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_TRACK_ID = re.compile(r"^[A-Za-z0-9]{22}$")


def evaluate(
    player_state: Mapping[str, Any],
    diagnostic: Mapping[str, Any],
    later_state: Mapping[str, Any] | None = None,
) -> list[str]:
    """Return every reason this is not proven Spotify playback. Empty means proven.

    ``later_state`` is a second read of the player a few seconds after the first.
    """
    problems: list[str] = []
    now = player_state.get("now_playing") or player_state.get("current") or {}
    provider = str(now.get("provider") or "").lower()
    if provider not in {"spotify", "spotify_cdp"}:
        problems.append("player provider is %r, not Spotify" % (provider or None))
    if now.get("video_id"):
        problems.append("the item has a YouTube video id")
    if not player_state.get("is_playing"):
        problems.append("the player does not report playing")
    if not str(now.get("title") or "").strip():
        problems.append("the player has no title for the song")
    if not now.get("artwork_url"):
        problems.append("the player has no cover for the song")
    if later_state is not None:
        before = float(player_state.get("position") or 0)
        after = float(later_state.get("position") or 0)
        if after <= before:
            problems.append("the player's position did not move (%s s then %s s)" % (before, after))
        if not later_state.get("is_playing"):
            problems.append("the player stopped reporting playing on the second read")
    if not _TRACK_ID.match(str(diagnostic.get("track_id") or "")):
        problems.append("no Spotify track id from the web player")
    if not diagnostic.get("position_moving"):
        problems.append("the position is not moving")
    audio = diagnostic.get("audio") or {}
    if not audio.get("session_found"):
        problems.append("the hidden browser has no Windows audio session")
    elif not audio.get("peak"):
        problems.append("the hidden browser's audio session is silent")
    failure = diagnostic.get("first_failure")
    if failure != "none":
        problems.append("diagnostic first_failure=%s" % failure)
    return problems


def _get(port: int, path: str, api_key: str) -> dict[str, Any]:
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), headers={"X-API-Key": api_key})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8756)
    parser.add_argument("--api-key-file", type=Path, required=True)
    args = parser.parse_args(argv)
    api_key = args.api_key_file.read_text(encoding="utf-8").strip()
    state = _get(args.port, "/v1/player/state", api_key)
    diagnostic = _get(args.port, "/v1/spotify/diagnostic", api_key)
    time.sleep(3)
    later = _get(args.port, "/v1/player/state", api_key)
    problems = evaluate(state, diagnostic, later)
    if problems:
        print("FAIL: " + "; ".join(problems))
        return 1
    audio = diagnostic.get("audio") or {}
    print(
        "PASS: Spotify track %s, position moved %ss, browser audio peak %s"
        % (diagnostic.get("track_id"), diagnostic.get("position_moved_s"), audio.get("peak"))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
