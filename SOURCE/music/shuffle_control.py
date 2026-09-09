"""The single authority for applying shuffle: the preference and the play order together.

Why this module exists (#4214). Shuffle used to be two unrelated halves that no
single call site owned:

* the *preference* ``preferences.shuffle``, written by anyone who felt like it
  (``intent/instant_commands/music.py``, ``skills/builtin/music_control.py``,
  ``core/compat.StateCompat.set_shuffle``), and
* the *play order*, reordered by exactly one place in the whole product -- the
  body of the ``POST /v1/shuffle`` route in ``ui/api/routes/control.py``.

Nothing connected them. ``preferences.shuffle`` carried no ordering semantics at
all: every reader of it (``core/state_selectors.select_shuffle_enabled``,
``core/compat``, ``models/state_manager``, ``utils/api_helpers.inject_preferences``,
the state broadcast) only reports it back out. So every writer that was not the
HTTP route set a flag that changed nothing and answered "Shuffle is now on" --
a display-only flag, and a false success in exactly the shape #2757 exists to
stop.

The fix is structural rather than per-call-site: the reorder lives *behind* the
preference write, in one function, and every writer goes through it. Two
properties are load-bearing and are what the tests pin:

1. **Reorder first, record second.** A reorder that cannot happen raises
   `ShuffleNotApplied`; the preference is left untouched, so a caller can never
   report a shuffle that did not happen, and the reported flag never diverges
   from the real play order.
2. **One resolution of the queue engine.** `resolve_queue_engine` is the only
   place that walks from a music player to its `PlaylistQueueEngine`, so no
   other caller has to reach through ``player._playlist`` for a cursor method
   the wrapper may not re-export (the #2757 bug class, guarded by
   ``scripts/check_queue_engine_cursor_pokes.py``).

An empty queue is NOT a failure: a user who turns shuffle on with nothing
playing is expressing a preference for what comes next, and that must be
recorded. `ShuffleResult.queue_available` distinguishes "there was no queue" from
"the queue was reordered" for callers that want to say so.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from typing import Any, NamedTuple, TypeVar

from core.logging_config import get_logger

log = get_logger(__name__)

__all__ = [
    "ShuffleNotApplied",
    "ShuffleResult",
    "apply_shuffle",
    "resolve_queue_engine",
    "restore_saved_order",
]

_Item = TypeVar("_Item")


def restore_saved_order(
    saved_ids: Sequence[str] | None,
    items: Iterable[_Item],
    *,
    key: Callable[[_Item], str] = lambda item: item.id,  # type: ignore[attr-defined]
) -> list[_Item]:
    """Reorder ``items`` back to ``saved_ids``, keeping every item.

    This is the un-shuffle rule for the whole product, in one place, because
    both surfaces need exactly the same three properties and getting any of
    them wrong is a data-loss bug rather than a cosmetic one:

    1. **Nothing is dropped.** An item whose id is not in ``saved_ids`` was
       queued after the shuffle, so it has no saved position. It keeps its
       current relative order and follows the restored run. Deleting it -- the
       pre-#4214 desktop behaviour -- silently empties part of the user's
       queue, which is worse than the wrong order un-shuffle exists to fix.
    2. **Nothing is invented.** A saved id no longer present (the track played
       while shuffle was on) is skipped rather than resurrected.
    3. **Duplicates survive by count.** Two copies of one track are two items,
       not one. Matching on a plain id set collapses them and loses a copy.

    Args:
        saved_ids: Ids in their pre-shuffle order, or None/empty for no saved
            order (in which case ``items`` is returned unchanged).
        items: The queue as it stands now.
        key: How to read an item's id. Defaults to the ``.id`` attribute.

    Returns:
        A new list holding exactly the items given, reordered.
    """
    current = list(items)
    if not saved_ids:
        return current

    by_id: dict[str, list[_Item]] = defaultdict(list)
    for item in current:
        by_id[key(item)].append(item)

    restored: list[_Item] = []
    for saved_id in saved_ids:
        bucket = by_id.get(saved_id)
        if bucket:
            restored.append(bucket.pop(0))

    # Whatever the saved order did not claim was added after the shuffle; it
    # follows, in the order it is in now.
    taken = {id(item) for item in restored}
    restored.extend(item for item in current if id(item) not in taken)
    return restored


class ShuffleNotApplied(RuntimeError):
    """The play order could not be changed, so the preference was not recorded.

    Raised instead of returning a partial result: the caller's next move is to
    surface a failure to the user, never to answer success. See the module
    docstring for why a silent failure here is the entire bug.
    """


class ShuffleResult(NamedTuple):
    """What `apply_shuffle` actually did."""

    enabled: bool
    """The shuffle preference now in effect."""

    reordered: int
    """How many upcoming tracks changed position (0 when the queue was empty)."""

    queue_available: bool
    """Whether a live queue engine was reachable at all."""


def resolve_queue_engine(music: Any) -> Any | None:
    """Return the `PlaylistQueueEngine` behind a music player, or None.

    Accepts either the player itself or a wrapper exposing ``.player`` (the
    route hands us the latter, the instant-command and skill paths the former).
    Returns None -- not an error -- when no queue engine exists yet: that is the
    ordinary state before anything has played.

    The object's OWN engine wins over the one behind ``.player``. Nothing in
    the product has both -- `MusicPlayer` owns `_playlist` and exposes no
    `.player`, and the wrappers are the other way round -- so the order costs
    nothing in production, and it stops a caller that holds the real player
    from being walked past it into some unrelated `.player` attribute.
    """
    if music is None:
        return None
    for candidate in (music, getattr(music, "player", None)):
        if candidate is None:
            continue
        engine = getattr(candidate, "_playlist", None)
        if engine is not None:
            return engine
    return None


def _reorder(engine: Any, enabled: bool) -> int:
    """Apply the play-order half. Raises `ShuffleNotApplied` if it cannot."""
    method_name = "shuffle_upcoming" if enabled else "unshuffle_upcoming"
    method = getattr(engine, method_name, None)
    if not callable(method):
        raise ShuffleNotApplied("queue engine %s does not expose %s" % (type(engine).__name__, method_name))
    try:
        count = method()
    except Exception as exc:
        raise ShuffleNotApplied("%s failed: %s" % (method_name, exc)) from exc
    return int(count or 0)


def _record_preference(music: Any, enabled: bool, *, user_id: str | None) -> None:
    """Record the preference in both stores that report shuffle back out.

    Two stores really do exist and both are read: the StateHub preferences
    (what ``select_shuffle_enabled`` / the state broadcast serve to the UI) and
    the player's `ConsolidatedState` (what ``models/state_manager.snapshot``
    serves). Writing only one is why the instant-command path could not even
    make the *displayed* toggle move.
    """
    from core.compat import StateCompat

    StateCompat(user_id=user_id).set_shuffle(enabled)

    state_mgr = getattr(music, "_consolidated_state", None)
    if state_mgr is None:
        player = getattr(music, "player", None)
        state_mgr = getattr(player, "_consolidated_state", None)
    if state_mgr is not None:
        setter = getattr(state_mgr, "set_shuffle", None)
        if callable(setter):
            setter(enabled)


def apply_shuffle(music: Any, enabled: bool, *, user_id: str | None = None) -> ShuffleResult:
    """Turn shuffle on or off for real: reorder the queue, then record the preference.

    Args:
        music: The music player (or a wrapper exposing ``.player``).
        enabled: True to shuffle the upcoming queue, False to restore the
            pre-shuffle order.
        user_id: Owner of the preference, for the user-scoped state hub.

    Returns:
        `ShuffleResult` describing what happened.

    Raises:
        ShuffleNotApplied: the play order could not be changed. The preference
            is deliberately left untouched so the reported state stays true.
    """
    enabled = bool(enabled)
    engine = resolve_queue_engine(music)

    reordered = 0
    if engine is not None:
        reordered = _reorder(engine, enabled)

    _record_preference(music, enabled, user_id=user_id)

    log.info(
        "Shuffle %s (reordered %d upcoming tracks, queue_available=%s)",
        "on" if enabled else "off",
        reordered,
        engine is not None,
    )
    return ShuffleResult(enabled=enabled, reordered=reordered, queue_available=engine is not None)
