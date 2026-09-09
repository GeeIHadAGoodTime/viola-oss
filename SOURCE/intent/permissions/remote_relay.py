"""Consent posture for a whole-turn relay from a remote principal.

WHY THIS EXISTS
---------------
The cloud can hand a user's desktop either of two very different things:

* a BOUNDED capability request -- ``desktop.screenshot``,
  ``smart_home.ha_control`` -- one action, typed arguments, typed result; or
* ``agent.dispatch``, which relays an ENTIRE natural-language turn and lets
  the desktop's own agent pipeline decide what to do with it.

``services/companion/security.py`` requires an explicit, expiring consent
grant for the first kind (``requires_explicit_consent`` -> ``assert_consent``
in ``services/companion/bridge.py``). It required nothing for the second,
even though the second is a strict superset: a relayed turn reaches the same
desktop agent that can drive ``computer``, ``run_command``, ``file_write``,
and the smart home. So the consent gate was bypassable by PHRASING -- ask for
the bounded action and you need a consent session, ask for the whole turn and
you did not.

WHY THE FIX IS HERE AND NOT AT THE BRIDGE
-----------------------------------------
Requiring a consent session for ``agent.dispatch`` itself would have closed
the hole by breaking the feature: the cloud->desktop auto-link relay is
default-on and forwards EVERY turn of a signed-in user with a desktop online
(``services/cloud_intent/dispatch.py``), while consent grants are short-lived
(``TIMEOUT_5_MINUTES``). Every cloud turn would have started failing until
the user re-consented, minutes apart.

The hole is not that a turn is relayed. It is that relaying it silently
conferred powers the bounded path gates. So the relay stays open and
unconsented -- ask a question, play music, check the calendar -- and the
consent-gated POWERS stay gated inside it. That is parity, reached without
regressing a shipped default.

SCOPE IS DELIBERATELY PARITY, NOT MORE
--------------------------------------
This set mirrors what ``requires_explicit_consent`` already gates: the
``desktop.*`` scope (control of the machine) and ``smart_home.ha_control``.
Read-only ``file_read`` is deliberately NOT here: its bounded companion twin
is scope ``files``, which is not consent-gated today, so adding it would be a
new product restriction rather than the parity this closes. That asymmetry
(a remote principal can still READ the desktop's disk through a relayed turn)
is real and is surfaced as a separate finding rather than decided here.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar, Token

# Tool names that correspond to the consent-gated companion scopes. Matching
# is on the tool the desktop agent would actually call, because that is the
# thing the remote principal ends up reaching.
CONSENT_GATED_RELAY_TOOLS: frozenset[str] = frozenset(
    {
        # desktop.* scope -- driving the user's machine.
        "computer",
        "run_command",
        "file_write",
        # ALIASES for `computer`. These are separately registered tool names
        # whose bodies call the computer-use implementation as a plain Python
        # function, so the hub is never asked to approve "computer" and a
        # name-only check misses them entirely. `desktop_interact` covers
        # focus/click/type/key/hotkey/mouse_click/scroll and is annotated
        # CONFIRM (hence auto-approved); `desktop_volume` changes the
        # machine-wide output volume. Matching on the power rather than on one
        # spelling of it is the whole point -- the `remote-relay-consent-parity`
        # gate scans for any registered tool that reaches `computer` and fails
        # if it is not listed here, so a future alias cannot reopen this.
        "desktop_interact",
        "desktop_volume",
        # smart_home.ha_control -- acting on the user's home.
        "smart_home",
        # Desktop Add Room pairing drives the local hub's audio devices.
        "pair_speaker_setup",
    }
)

_UNCONSENTED_REMOTE_RELAY: ContextVar[bool] = ContextVar(
    "viola_unconsented_remote_relay",
    default=False,
)


def is_unconsented_remote_relay() -> bool:
    """Whether the current turn was relayed by a principal without consent."""
    return _UNCONSENTED_REMOTE_RELAY.get() is True


def relay_consent_blocks(tool_name: str) -> bool:
    """Whether ``tool_name`` must be refused for the current relayed turn."""
    if not is_unconsented_remote_relay():
        return False
    return str(tool_name or "").strip() in CONSENT_GATED_RELAY_TOOLS


def relay_consent_denial_reason(tool_name: str) -> str:
    """User-facing explanation for a consent-gated refusal."""
    return (
        "'%s' controls this computer, and this turn was relayed from another device. "
        "Viola requires an explicit desktop-control consent grant for that, the same one "
        "the direct remote-control path requires. Ask the user to approve desktop control "
        "for this device, or to run the request on the computer itself." % tool_name
    )


@contextlib.contextmanager
def unconsented_remote_relay() -> Iterator[None]:
    """Mark everything executed inside as an unconsented relayed turn.

    Wrapping the pipeline call (rather than tagging the request object) is
    what makes this hold for tools reached indirectly -- a subtask, a routine,
    a delegated provider -- since they all run inside this same context.
    """
    token: Token[bool] = _UNCONSENTED_REMOTE_RELAY.set(True)
    try:
        yield
    finally:
        _UNCONSENTED_REMOTE_RELAY.reset(token)
