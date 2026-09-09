"""
Instant command patterns - regex patterns for instant command matching.

Each pattern in this module bypasses the LLM. If the regex is wrong,
the model never gets to course-correct, which causes user-facing
regressions that are hard to revert. Every pattern here MUST satisfy:

1. Latency win: LLM round-trip would be measurably slower (≥3s).
2. Near-zero false-positive risk: regex matches ONLY the intended intent.
3. No semantic overlap: doesn't compete with another instant pattern or
   with a clearly-better LLM-tool path.
4. LLM path is inadequate: no equivalent tool exists OR the LLM tool path
   consistently fails this intent.
5. Edge-case test coverage: tests cover known false-positive triggers.

Founder rule (2026-04-27): "minimum regex/instant commands; the ones we
do have should be low risk, high ROI, and have a high bar of acceptance."

DO NOT add a pattern without checking these criteria. DO NOT remove a
pattern without first proving the LLM-tool path handles its intent.
"""

from __future__ import annotations

import re
from re import Pattern
from typing import Any

# CB-14: Common polite prefix for playback commands.
# Matches optional politeness words that voice-to-text often prepends:
#   "please", "can you", "could you", "go ahead and", "hey viola"
POLITE_PREFIX = r"(?:(?:please|can\s+you|could\s+you|go\s+ahead\s+and|hey\s+viola)\s+)?"
# Backwards-compatible alias for in-module use.
_POLITE = POLITE_PREFIX

# Shared negative lookahead so a negated control phrase ("don't stop",
# "won't pause") never bypasses the model -- every exact-phrase instant
# pattern in this module reuses the same rejection list (#1402).
_NEGATION_LOOKAHEAD = r"(?!(?:don['\u2019]?t|do\s+not|won['\u2019]?t|cannot|can['\u2019]?t)\s+)"

# Compiled smart-stop regex. Exported so the post-LLM CB-6 false-stop guard
# in pipeline_processors.py can use the same pattern instead of a divergent
# mirror copy (the previous mirror was missing "turn off the music",
# "that is enough", and the negative-lookahead \u2014 letting CB-6 reject genuine
# stop intents the instant pattern would have accepted).
SMART_STOP_REGEX = re.compile(
    r"^" + POLITE_PREFIX + _NEGATION_LOOKAHEAD + r"(?:stop"
    r"(?:\s+(?:playing|music|playback|it|that|this|the\s+(?:music|song|track|audio)))?"
    r"|halt|emergency\s+stop"
    r"|turn\s+off\s+(?:the\s+)?(?:music|song|audio|playback)"
    r"|that(?:['\u2019]?s|\s+is)\s+enough"
    r")(?:\s+please)?[!.]*$",
    re.I,
)

# #1402: "pause" went through the full model round-trip (15-21s, and once
# flailed into ask_user/computer-use tools) while "stop" short-circuited via
# SMART_STOP_REGEX above (~1.2s). CLAUDE.md's own carve-out example names
# "pause, stop" together, and the pause executor already existed, unwired,
# at intent/instant_commands/music.py:MusicHandlersMixin.pause. Unlike
# smart_stop -- a latency/safety-critical universal halt that deliberately
# accepts "stop it" / "stop the music" / "turn off the music" -- pause has no
# such safety urgency, so it holds the tighter end of the bar: the bare exact
# word only (plus the shared polite wrapper), NOT "pause it" or "pause the
# music". Any extra word after "pause" routes to the model, per CLAUDE.md's
# "any extra words around an exact phrase route to the model too".
PAUSE_REGEX = re.compile(
    r"^" + POLITE_PREFIX + _NEGATION_LOOKAHEAD + r"pause(?:\s+please)?[!.]*$",
    re.I,
)

# #1402: bare "resume" is the exact-phrase inverse of pause, at the same bar.
# Checked before adding: "resume" has no other command meaning anywhere in
# Viola's instant/tool vocabulary (no agent-task resume, no timer resume --
# grep confirms the only other "resume" callsite is an unrelated task-trace
# checkpoint method), so the bare word is unambiguous. "unpause" was
# considered and NOT added: the linked audit only measured pause vs stop
# latency, there is no live evidence "unpause" causes the same round-trip
# pain, and CLAUDE.md's bar is "when in doubt, it is not an instant command" --
# it can be added later with its own evidence. Same tight discipline as
# pause: the bare word only, no "resume playing" / "resume the music".
RESUME_REGEX = re.compile(
    r"^" + POLITE_PREFIX + _NEGATION_LOOKAHEAD + r"resume(?:\s+please)?[!.]*$",
    re.I,
)

# Instant command patterns - execute immediately without AI.
#
# DEBOX (2026-05-29, boxing audit W2 intent lane) + #1402 (2026-07-16): the
# entries that survive here are smart_stop, pause, and resume. CLAUDE.md
# permits exactly ONE deliberate instant-command exception - a tiny allowlist
# of unambiguous, zero-argument control phrases (its own worked example is
# "pause, stop") that short-circuit for latency/safety. It is an exact-
# control-phrase boundary, NOT semantic intent classification.
#
# volume_up / volume_down / get_time_date / play_default_playlist were removed
# because each was a semantic natural-language routing ladder that pre-empted
# the model: "make it louder" / "lower the volume a bit" / "what time is it" /
# "play some music" are intent classifications over varied phrasings, not exact
# control-word matches. The model handles volume, time, and default-playback
# natively as tools; routing those through a regex ladder is the boxing class
# CLAUDE.md forbids. Claude Code TS has no natural-language intent ladder - it
# resolves explicit slash commands by name and lets the model pick every other
# tool from its schema.
InstantPattern = tuple[Pattern[str], str, dict[str, Any], str]
INSTANT_PATTERNS: list[InstantPattern] = [
    # ===== UNIVERSAL STOP (smart_stop) - the one deliberate exception =====
    # RATIONALE: Latency- and safety-critical. Users hit "stop" when something
    # is wrong; they need playback halted NOW even if the LLM is slow or down.
    # The negative lookahead rejects negated forms like "don't stop" / "won't
    # stop". Narrow regex, high frequency, executor wired. This is the CLAUDE.md
    # "pause/stop" control-phrase carve-out.
    (
        SMART_STOP_REGEX,
        "smart_stop",
        {},
        "Smart stop (most recent activity)",
    ),
    # ===== PAUSE (#1402) =====
    # Five-criteria check (module header above): (1) latency win - measured
    # 15-21s LLM round-trip vs ~1.2s for stop's instant path, same pipeline;
    # (2) near-zero false-positive - bare exact word only, no "it"/"the music"
    # broadening; (3) no semantic overlap - doesn't compete with smart_stop or
    # any other instant pattern; (4) LLM path inadequate - the linked probe
    # shows it nondeterministically flailed into ask_user/computer-use tools
    # for a zero-argument control word; (5) edge-case coverage - see
    # tests/unit/test_instant_commands_patterns.py (negation, extra-word,
    # and polite-prefix cases).
    (
        PAUSE_REGEX,
        "pause",
        {},
        "Pause playback",
    ),
    # ===== RESUME (#1402) =====
    # Same five-criteria bar as pause, at the exact-phrase inverse. See
    # RESUME_REGEX's own comment above for why "unpause" was evaluated and
    # NOT added (no live evidence yet).
    (
        RESUME_REGEX,
        "resume",
        {},
        "Resume playback",
    ),
]
