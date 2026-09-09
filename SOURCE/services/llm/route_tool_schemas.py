"""Route-mode tool schemas for native function calling.

Defines route action tools plus answer/ignore response tools as structured
tool schemas. These replace the JSON-in-prompt approach for
providers that support native function calling (OpenAI, Anthropic).

The schemas are stored in a neutral internal format (ROUTE_TOOL_SCHEMAS) and
converted to provider-specific formats via helper functions.

Source of truth: docs/LLM_CAPABILITY_MAP.md
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Neutral schema definitions (provider-agnostic)
# ---------------------------------------------------------------------------
# Each entry: {"name": str, "description": str, "parameters": dict}
# Parameters follow JSON Schema (type, properties, required).

ROUTE_TOOL_SCHEMAS: list[dict[str, Any]] = [
    # ===== MUSIC PLAYBACK & TRANSPORT =====
    {
        "name": "media",
        "description": (
            "Unified music and video search/play tool. For direct requests like 'play Drake', use "
            "action='search_play' with query to search, select the top playable candidate, and start playback in "
            "one tool call. Search mode: pass query to inspect playable candidates with "
            "provider, title, artist, and track_uri. For broad requests — single-word artist names ('drake'), "
            "bare genres/moods/eras ('jazz', 'chill', '80s'), or 'play me a X' / 'play some X' patterns — "
            "action='search_play' is preferred unless the user is asking to choose from options. Pass query as "
            "just the descriptor (artist/title/genre/mood) without filler words like 'some' or 'play me'; for a "
            "fully generic 'play some music' / 'play anything' request with no descriptor, use search_play with an "
            "empty query so the tool returns a general selection rather than searching the literal word. "
            "Use CURRENT SYSTEM STATE for provider choice, local-library metadata, local-match facts, "
            "target_room, room_route/pairing_flow, recently played tracks, and user music preferences. "
            "Setup/login requests use pair_speaker_setup or connect_music_provider. Transport controls use playback."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The core music descriptor to search for — an artist, title, genre, or mood — "
                        "for example 'Miles Davis', 'Kind of Blue', 'Sade - Smooth Operator', "
                        "'upbeat 80s rock', or 'lofi beats'. Pass ONLY the descriptor, stripped of "
                        "conversational filler: 'play some jazz' -> query 'jazz' (not 'some jazz'), "
                        "'put on some Drake' -> 'Drake'. Filler like 'some' or 'play me' left in the "
                        "query matches a track literally titled that (e.g. a song named 'Some Music'). "
                        "For a fully generic request with no descriptor at all — 'play some music', "
                        "'play anything', 'put on music', 'play me something' — leave query EMPTY: "
                        "search_play then returns a general selection instead of searching a filler word. "
                        "Otherwise leave empty only when playing a selected track_uri. "
                        "Keep room names in target_room."
                    ),
                },
                "action": {
                    "type": "string",
                    "enum": [
                        "auto",
                        "search",
                        "play",
                        "search_play",
                    ],
                    "description": (
                        "Media action. auto preserves compatibility: query searches and track_uri plays. "
                        "Use search_play for direct 'play X' requests to search, select the top candidate, "
                        "and start playback in one tool call."
                    ),
                },
                "track_uri": {
                    "type": "string",
                    "description": (
                        "Exact candidate identifier from a prior media search result. When set, media plays "
                        "this selected item instead of searching."
                    ),
                },
                "target_room": {
                    "type": "string",
                    "description": (
                        "Optional room or room-group name for multi-room playback, "
                        "for example 'kitchen' from 'play music in the kitchen'. "
                        "This routes through Viola's in-house multi-room speaker "
                        "registry first. If no room is paired, the executor plays "
                        "locally and returns Add Speaker QR pairing_flow facts."
                    ),
                },
                "provider": {
                    "type": "string",
                    "enum": [
                        "auto",
                        "any",
                        "spotify",
                        "youtube",
                        "youtube_music",
                        "local",
                    ],
                    "description": (
                        "Optional provider override. Use 'auto' by default so the active music provider setting "
                        "drives search/playback. Set explicitly when the user names a provider ('from spotify', "
                        "'on youtube'), OR when the music route context shows 0/low local matches for a "
                        "genre/mood/era request and another provider is authenticated."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum candidates to return in search mode. Use a small number for focused choices.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "pair_speaker_setup",
        "description": (
            "Open Viola's Add Room speaker setup flow without starting playback. "
            "Use for setup-only requests like 'add a kitchen speaker', "
            "'set up a speaker', 'pair a new room', or 'connect a bedroom speaker'. "
            "Returns pairing_flow, qr_data, pairing_code, spoke_url, and UI action facts "
            "for the Rooms > Add Room QR card. Do not use media or playback for "
            "these setup-only requests unless the user also asked to start music."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target_room": {
                    "type": "string",
                    "description": (
                        "Room or speaker name to prefill in the Add Room flow, for example "
                        "'kitchen' from 'add a kitchen speaker'. Use 'speaker' if no room "
                        "name was provided."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "open_app_panel",
        "description": (
            "Open a Viola UI panel for requests like 'open settings', 'show payment methods', "
            "or 'take me to my calendar'. Available panel_id values: settings, "
            "rooms.add_speaker, calendar, music_accounts, payment_methods, help. "
            "Returns structured ui_action data for the app; this is a UI capability, "
            "not a classifier, gate, or settings mutation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "panel_id": {
                    "type": "string",
                    "enum": [
                        "settings",
                        "rooms.add_speaker",
                        "calendar",
                        "music_accounts",
                        "payment_methods",
                        "help",
                    ],
                    "description": "Panel to open.",
                },
                "sub_tab": {
                    "type": "string",
                    "description": (
                        "Optional Settings tab such as account, ai_agents, music_voice, "
                        "messaging, services, payment, or preferences."
                    ),
                },
                "prefill": {
                    "type": "object",
                    "description": "Optional UI prefill, for example {'room_name': 'kitchen'}.",
                    "additionalProperties": True,
                },
            },
            "required": ["panel_id"],
        },
    },
    {
        "name": "play_next",
        "description": (
            "Queue a song to play after the current track. "
            'Use for "play X next", "add X after this". '
            'Do NOT use for "next" alone -- that is skip_track.'
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Song name, artist, or album to queue next",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "add_to_queue",
        "description": "Add a song to the playback queue.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Song name, artist, or album to add to queue",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "playback",
        "description": (
            "Playback transport control. "
            "Use action='pause' to pause current playback for 'pause', 'hold on', "
            "'wait a moment', 'enough music for now', 'shh'. "
            "Use action='resume' to resume paused playback for 'resume', "
            "'continue', 'go ahead', 'bring back the tunes'. "
            "Use action='stop' to stop playback completely ONLY for explicit "
            "'stop', 'stop music', or 'stop playing'. Do NOT use action='stop' "
            "for 'stop worrying', 'don't stop', 'stop thinking about it', "
            "'stop the car', or any phrase where 'stop' is figurative or "
            "refers to a non-music action -- those are conversation and should "
            "use the answer tool instead. "
            "Use action='skip' for 'next', 'skip', 'skip this song', "
            "'play the next song', 'I don't like this one', 'not feeling this'. "
            "Use action='previous' for 'previous', 'go back', "
            "'nah go back to the last one'. "
            "Use action='restart' to restart the current song from the "
            "beginning one time, no looping, ONLY for 'replay', 'start over', "
            "'from the top', 'play that again from scratch'. NEVER use "
            "action='restart' when the user says the word 'repeat' -- "
            "'repeat', 'repeat this song', 'on repeat' ALL mean repeat_one, "
            "not restart. "
            "Use action='seek' to jump to a specific position in the current "
            "track with seconds. Add Room setup and speaker pairing belong to "
            "pair_speaker_setup, not playback. Examples: playback(action='pause'), "
            "playback(action='seek', seconds=90)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["pause", "resume", "stop", "skip", "previous", "restart", "seek"],
                    "description": "Playback action to perform",
                },
                "seconds": {
                    "type": "integer",
                    "description": "Required for action='seek'. Position in seconds to jump to.",
                },
            },
            "required": ["action"],
        },
    },
    # ===== VOLUME (1) =====
    {
        "name": "volume",
        "description": (
            "Music player volume control for the active Viola music player. "
            "This changes the per-player music volume, not the machine-wide Windows/macOS/Linux output volume. "
            "Use CURRENT SYSTEM STATE: when music is currently playing, bare volume requests like louder, quieter, "
            "or turn it down apply here. Use desktop_volume only for explicit system, OS, desktop, speaker, "
            "computer, or master output volume requests. "
            "Use action='set' with level 0-100 for 'set volume to 50', "
            "'volume 75', 'set it to about half volume'. "
            "Use action='up' for 'louder', 'volume up', 'turn it up', "
            "'crank it', 'make it louder'. "
            "Use action='down' for 'quieter', 'volume down', 'turn it down', "
            "'way too loud', 'turn it down a bit'. "
            "Use action='mute' for 'mute', 'shh quiet for a minute'. "
            "Use action='unmute' to restore audio. "
            "Examples: volume(action='set', level=50), volume(action='up', step=10), "
            "volume(action='mute')."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["set", "up", "down", "mute", "unmute"],
                    "description": "Volume action to perform",
                },
                "level": {
                    "type": "integer",
                    "description": "Required for action='set'. Volume level from 0 to 100.",
                },
                "step": {
                    "type": "integer",
                    "description": "Optional for action='up' or action='down'. Volume step (default 10).",
                },
            },
            "required": ["action"],
        },
    },
    # ===== QUEUE & PLAYBACK MODES (1) =====
    {
        "name": "playback_mode",
        "description": (
            "Queue and playback mode control. "
            "Use action='shuffle_on' for 'shuffle on', 'shuffle my music', "
            "'mix it up', 'shuffle please'. "
            "Use action='shuffle_off' for 'shuffle off', 'stop shuffling'. "
            "Use action='repeat_all' to repeat all tracks in the queue for "
            "'repeat all', 'loop the queue'. "
            "Use action='repeat_one' to loop the current song continuously. "
            "IMPORTANT: Any time the user says 'repeat' this is the correct "
            "action. Use for 'repeat', 'repeat this song', 'repeat this', "
            "'put this on repeat', 'loop this', 'keep playing this', "
            "'on repeat'. The word 'repeat' ALWAYS means action='repeat_one', "
            "never action='restart'. Only 'replay' or 'start over' "
            "(without the word 'repeat') mean playback(action='restart'). "
            "Use action='repeat_off' to disable repeat mode. "
            "Use action='clear_queue' to clear the playback queue."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "shuffle_on",
                        "shuffle_off",
                        "repeat_all",
                        "repeat_one",
                        "repeat_off",
                        "clear_queue",
                    ],
                    "description": "Playback mode action to perform",
                },
            },
            "required": ["action"],
        },
    },
    # ===== FEEDBACK (1) =====
    {
        "name": "rate_track",
        "description": (
            "Rate the current song. "
            "Use rating='up' to like / thumbs up the current song for "
            "'I like this song', 'thumbs up', 'love this'. "
            "Use rating='down' to dislike / thumbs down the current song."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "rating": {
                    "type": "string",
                    "enum": ["up", "down"],
                    "description": "Track rating to apply",
                },
            },
            "required": ["rating"],
        },
    },
    # ===== PLAYLISTS (1) =====
    {
        "name": "playlist",
        "description": (
            "Playlist control for saved Viola playlists and liked-song playback. "
            "action='play' plays a saved playlist by name; match names from current system state when available. "
            "action='set_default' sets the default playlist. "
            "action='play_favorites' plays liked/favorited songs for requests like 'play my favorites' or 'play something I like'. "
            "action='create' creates a playlist from context, using inferred names such as workout, chill, or My Playlist when no name is given. "
            "provider='local' fits requests from local songs or local files. "
            "action='delete' removes a saved playlist by name. "
            "Does not answer general music facts or transport controls; media, playback, and web_search cover those."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["play", "set_default", "play_favorites", "create", "delete"],
                    "description": "Playlist action to perform",
                },
                "playlist_name": {
                    "type": "string",
                    "description": (
                        "Playlist name. Required for action='play', action='set_default', "
                        "action='create', and action='delete'. For action='create', infer "
                        "from context and use 'My Playlist' if no name is given."
                    ),
                },
                "shuffle": {
                    "type": "boolean",
                    "description": "Optional for action='play_favorites'. Whether to shuffle favorites (default true).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Optional for action='play_favorites'. Maximum number of favorites to play (default 15).",
                },
                "provider": {
                    "type": "string",
                    "description": (
                        "Optional for action='create'. Music provider to use " '(e.g. "local", "youtube").'
                    ),
                },
            },
            "required": ["action"],
        },
    },
    # ===== STATUS (2) =====
    {
        "name": "status",
        "description": (
            "Get current playback status. Only use if CURRENT SYSTEM STATE "
            "lacks playback info. If playback info is already in the system "
            "context, answer directly using the answer tool instead."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "help",
        "description": (
            "List available commands and capabilities. "
            "ALWAYS use this for 'what can you do?', 'help', 'list commands', "
            "'what are your capabilities'. Never answer these directly -- "
            "always use the help command."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    # ===== TIMERS, ALARMS, NOTIFICATIONS (3) =====
    {
        "name": "timer",
        "description": (
            "Timer control. "
            "action='set' creates a countdown timer with minutes and optional label. "
            "action='sleep_timer' schedules playback to stop after a duration. "
            "action='cancel' cancels the active countdown timer, action='cancel_all' cancels all countdown timers, "
            "and action='cancel_sleep_timer' to cancel pending sleep timers. "
            "action='status' checks active countdown timers. "
            "Countdown timers are separate from calendar events and recurring scheduled automations."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["set", "cancel", "cancel_all", "status", "sleep_timer", "cancel_sleep_timer"],
                    "description": "Timer action to perform",
                },
                "minutes": {
                    "type": "integer",
                    "description": "Required for action='set' and action='sleep_timer'. Duration in minutes.",
                },
                "label": {
                    "type": "string",
                    "description": "Optional timer or sleep-timer label.",
                },
                "timer_id": {
                    "type": "string",
                    "description": "Optional timer or sleep-timer id for cancellation.",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "alarm",
        "description": (
            "Alarm control. Use action='set' for clock-time alarms that should fire an alarm sound. "
            "Use action='cancel' to cancel a named alarm or alarm id, action='cancel_all' to cancel pending alarms, "
            "action='status' to list alarms, and action='sound' only for scheduler alarm dispatch."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["set", "cancel", "cancel_all", "status", "sound"],
                    "description": "Alarm action to perform.",
                },
                "when": {
                    "type": "string",
                    "description": "Required for action='set'. Alarm time such as '7 AM', 'tomorrow 8 AM', or an ISO datetime.",
                },
                "label": {
                    "type": "string",
                    "description": "Optional alarm name for setting or cancelling a named alarm.",
                },
                "alarm_id": {
                    "type": "string",
                    "description": "Optional scheduler/alarm id for action='cancel'.",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "notify",
        "description": (
            "Send or schedule a notification to the user. Use this when the user asks to be notified or reminded. "
            "Provide a concise message, optional when for delayed delivery, and optional channel. "
            "User-scoped delivery uses subscribed web push devices; phone/SMS delivery requires an account-bound "
            "notification path."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Notification or reminder content.",
                },
                "when": {
                    "type": "string",
                    "description": "Optional delivery time such as 'in 5 minutes' or an ISO datetime.",
                },
                "channel": {
                    "type": "string",
                    "description": "Optional delivery channel. Phone/SMS requires an account-bound notification path.",
                },
            },
            "required": ["message"],
        },
    },
    # ===== CALENDAR (1) =====
    {
        "name": "calendar",
        "description": (
            "Calendar control. "
            "Calendar has an always-on local primary calendar; Google, Microsoft, and CalDAV are optional sync targets. "
            "action='today' gets today's calendar events. "
            "action='next' gets the next upcoming calendar event. "
            "action='create' creates a calendar event with title and time. "
            "action='delete' deletes a calendar event by title or event_id. "
            "Calendar read results include calendars_connected, connected_providers, and events. "
            "With the local provider connected, events=[] is an empty schedule for the requested range. "
            "Do not present Google/Microsoft/CalDAV setup as a prerequisite; remote connection only enables sync. "
            "Does not handle countdown timers or recurring Viola automations; timer and schedule cover those. "
            "Examples: calendar(action='today'), calendar(action='next'), "
            "calendar(action='create', title='Lunch', time='tomorrow 3pm'), "
            "calendar(action='delete', title='Dentist appointment')."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["today", "tomorrow", "week", "next", "create", "delete"],
                    "description": "Calendar action to perform",
                },
                "title": {
                    "type": "string",
                    "description": "Required for action='create'. Optional for action='delete' as an alternative to event_id.",
                },
                "time": {
                    "type": "string",
                    "description": "Required for action='create'. Event time (e.g. 'tomorrow 3pm', '2026-03-05 14:00').",
                },
                "event_id": {
                    "type": "string",
                    "description": "Optional for action='delete'. Event ID to delete as an alternative to title.",
                },
            },
            "required": ["action"],
        },
    },
    # ===== OTHER (1) =====
    {
        "name": "screenshot",
        "description": "Take a screenshot.",
        "parameters": {"type": "object", "properties": {}},
    },
    # ===== NON-COMMAND RESPONSES (2) =====
    {
        "name": "answer",
        "description": (
            "Use for final answers and genuine clarifying questions. "
            "This is for pure knowledge questions ('What is X?'), greetings, "
            "emotional support, jokes, math, or translations. "
            "Do NOT use for play/pause/skip/volume/timer/alarm/notify/calendar/screenshot requests "
            "-- those have dedicated command tools that MUST be used instead. "
            "If the user mentions music, playing, stopping, skipping, volume, "
            "or any action, use the matching command tool, NOT this one. "
            "In agent mode: NEVER use this to ask permission ('Want me to proceed?', "
            "'Should I go ahead?', 'Would you like me to...'). If the user already "
            "requested a task, use tools to execute it -- call this only for the "
            "final result. Before answering: verify the result — did the tool "
            "calls succeed? Is the user's request actually fulfilled? Do NOT "
            "answer with 'I'll try' or 'Let me attempt' — either do the task "
            "or explain why you can't. "
            "Keep answers to 1-2 sentences when spoken. Write for the ear (spoken via TTS). "
            "No markdown, no code fences. "
            "When your answer includes URLs, detailed lists, or information better read than heard, "
            "put the full details in a card and provide a brief voice_summary for TTS. "
            "Never read URLs aloud — reference the card instead. "
            "If there's a natural follow-up action the user might want, include it "
            "in suggest_followup. Keep it to one short question. Only suggest when "
            "it's genuinely useful — don't suggest on every task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "answer": {
                    "type": "string",
                    "description": "The conversational response text (1-2 sentences, spoken via TTS)",
                },
                "voice_summary": {
                    "type": "string",
                    "description": (
                        "Brief 1-sentence spoken version when the full answer is long, "
                        "contains URLs, or has a card. Spoken via TTS instead of the "
                        "full answer. Examples: 'I found 5 options, check the card.' "
                        "'Got the link you wanted, it is on your screen.' "
                        "Omit for short answers that work well spoken as-is."
                    ),
                },
                "continue_listening": {
                    "type": "boolean",
                    "description": (
                        "Set true when your response requires user input to proceed: a clarifying question, "
                        "confirmation request, presented options, or missing required action data such as an "
                        "E.164 phone number for a requested call. Set false only for terminal answers that "
                        "do not need another user reply before the task can continue. Never set false when "
                        "you ask the user to provide a required value."
                    ),
                },
                "suggest_followup": {
                    "type": "string",
                    "description": (
                        "Optional short follow-up question for the user. "
                        "Example: after sending email → 'Want me to set a reminder to follow up?' "
                        "After booking → 'Should I add this to your calendar?' "
                        "Leave empty or omit if no natural follow-up exists."
                    ),
                },
                "card": {
                    "type": "object",
                    "description": (
                        "Structured data card shown on screen alongside voice answer. "
                        "Use when response has 3+ items or structured data. Types: "
                        "list ({type:'list', title:'...', items:['...']}), "
                        "info ({type:'info', title:'...', value:'...', unit:'...'}), "
                        "table ({type:'table', title:'...', columns:['...'], rows:[['...']]}), "
                        "detail ({type:'detail', title:'...', body:'...', source:'...'}). "
                        "All types support optional subtitle shown below title."
                    ),
                    "properties": {
                        "type": {
                            "type": "string",
                            "description": "Card renderer type, such as list, info, table, or detail.",
                        },
                        "title": {
                            "type": "string",
                            "description": "Short card title shown on screen.",
                        },
                        "subtitle": {
                            "type": "string",
                            "description": "Optional secondary line shown below the title.",
                        },
                        "items": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List-card items.",
                        },
                        "value": {
                            "type": "string",
                            "description": "Info-card primary value.",
                        },
                        "unit": {
                            "type": "string",
                            "description": "Optional unit label for an info-card value.",
                        },
                        "columns": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Table-card column labels.",
                        },
                        "rows": {
                            "type": "array",
                            "items": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "description": "Table-card rows, each represented as a list of cell strings.",
                        },
                        "body": {
                            "type": "string",
                            "description": "Detail-card body text.",
                        },
                        "source": {
                            "type": "string",
                            "description": "Optional source label or URL for detail cards.",
                        },
                    },
                    # Cards are display envelopes; keep extension fields open for
                    # richer UI cards without forcing route-schema churn.
                    "additionalProperties": True,
                },
            },
            "required": ["answer"],
        },
    },
    {
        "name": "ignore",
        "description": (
            "Input is ambient noise, unclear speech, or not directed at the assistant. "
            "Only use when the transcript is CLEARLY ambient speech not directed at Viola: "
            "ongoing third-party conversation, podcast/TV audio, multiple speakers. "
            "Do NOT classify short statements or personal remarks as ignore -- the user "
            "already triggered the wake word. When in doubt between answer and ignore, "
            "always choose answer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Brief explanation of why the input was ignored",
                },
            },
            "required": ["reason"],
        },
    },
]

_COMPOUND_TOOLS: dict[str, dict[str, str]] = {
    "volume": {
        "set": "volume_set",
        "up": "volume_up",
        "down": "volume_down",
        "mute": "mute",
        "unmute": "unmute",
    },
    "playback": {
        "pause": "pause_music",
        "resume": "resume_music",
        "stop": "stop_music",
        "skip": "skip_track",
        "previous": "previous_track",
        "restart": "restart_track",
        "seek": "seek",
    },
    "playback_mode": {
        "shuffle_on": "shuffle_on",
        "shuffle_off": "shuffle_off",
        "repeat_all": "repeat_on",
        "repeat_one": "repeat_one",
        "repeat_off": "repeat_off",
        "clear_queue": "clear_queue",
    },
    "rate_track": {
        "up": "thumbs_up",
        "down": "thumbs_down",
    },
    "playlist": {
        "play": "play_saved_playlist",
        "set_default": "set_default_playlist",
        "play_favorites": "play_favorites",
        "create": "create_playlist",
        "delete": "delete_playlist",
    },
    "timer": {
        "set": "set_timer",
        "cancel": "cancel_timer",
        "cancel_all": "cancel_all_timers",
        "status": "timer_status",
        "sleep_timer": "set_sleep_timer",
        "cancel_sleep_timer": "cancel_sleep_timer",
    },
    "alarm": {
        "set": "set_alarm",
        "cancel": "cancel_alarm",
        "cancel_all": "cancel_all_alarms",
        "status": "alarm_status",
        "sound": "play_alarm_sound",
    },
    "calendar": {
        "today": "get_calendar_today",
        "tomorrow": "get_calendar_tomorrow",
        "week": "get_calendar_week",
        "next": "get_next_event",
        "create": "create_calendar_event",
        "delete": "delete_calendar_event",
    },
}

_COMPOUND_TOOL_SELECTOR_KEYS: dict[str, str] = {
    "rate_track": "rating",
}

# Pre-computed set of all route tool names for validation
_ROUTE_TOOL_NAMES: frozenset[str] = frozenset(t["name"] for t in ROUTE_TOOL_SCHEMAS)

# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------


def get_openai_route_tools() -> list[dict[str, Any]]:
    """Convert route schemas to OpenAI function calling format.

    Each tool is wrapped as::

        {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
    """
    result: list[dict[str, Any]] = []
    for schema in ROUTE_TOOL_SCHEMAS:
        result.append(
            {
                "type": "function",
                "function": {
                    "name": schema["name"],
                    "description": schema["description"],
                    "parameters": schema["parameters"],
                },
            }
        )
    return result


def get_anthropic_route_tools() -> list[dict[str, Any]]:
    """Convert route schemas to Anthropic tool format.

    Each tool has ``input_schema`` instead of ``parameters``::

        {"name": ..., "description": ..., "input_schema": ...}
    """
    result: list[dict[str, Any]] = []
    for schema in ROUTE_TOOL_SCHEMAS:
        result.append(
            {
                "name": schema["name"],
                "description": schema["description"],
                "input_schema": schema["parameters"],
            }
        )
    return result


def get_ask_only_tools_openai() -> list[dict[str, Any]]:
    """Return a subset of OpenAI tools for the ASK path (answer + ignore only)."""
    return [tool for tool in get_openai_route_tools() if tool["function"]["name"] in ("answer", "ignore")]


def get_ask_only_tools_anthropic() -> list[dict[str, Any]]:
    """Return a subset of Anthropic tools for the ASK path (answer + ignore only)."""
    return [tool for tool in get_anthropic_route_tools() if tool["name"] in ("answer", "ignore")]


_MUSIC_QUERY_TOOLS = frozenset({"media", "play_next", "add_to_queue"})

_MUSIC_QUERY_LEADING_FILLERS = (
    "me some",
    "us some",
    "me a",
    "us a",
    "me",
    "us",
    "some",
    "a",
    "an",
    "the",
    "any",
    "any of",
    "a bit of",
    "a little",
)


def _clean_music_query(query: str) -> str:
    """Strip leading conversational filler from music query text.

    The LLM commonly preserves filler from "play me some jazz" → "me some jazz".
    Local fuzzy search then matches the wrong track ("Drive Me Crazy"). Strip
    these determiners deterministically before the music backend sees them.
    """
    cleaned = query.strip()
    if not cleaned:
        return cleaned
    lowered = cleaned.lower()
    changed = True
    while changed:
        changed = False
        for filler in _MUSIC_QUERY_LEADING_FILLERS:
            prefix = filler + " "
            if lowered.startswith(prefix):
                cleaned = cleaned[len(prefix) :].lstrip()
                lowered = cleaned.lower()
                changed = True
                break
    return cleaned


def convert_route_tool_response(
    tool_name: str,
    tool_args: dict[str, Any],
) -> dict[str, Any]:
    """Convert a native tool call response back to Viola's internal dict format.

    The rest of Viola expects one of three dict shapes:
    - ``{"type": "answer", "answer": "...", "continue_listening": bool}``
    - ``{"type": "ignore", "reason": "..."}``
    - ``{"type": "tool_call", "tool": "...", "args": {...}, "continue_listening": false}``

    Args:
        tool_name: The function/tool name from the LLM response.
        tool_args: The parsed arguments dict.

    Compound route tools are expanded to the tool/action names that the rest of
    Viola already understands.

    Returns:
        A dict in Viola's expected format.
    """
    if tool_name == "answer":
        result: dict[str, Any] = {
            "type": "answer",
            "answer": tool_args.get("answer", ""),
            "continue_listening": bool(tool_args.get("continue_listening", False)),
        }
        # Pass through content card if provided
        card = tool_args.get("card")
        if card and isinstance(card, dict):
            result["card"] = card
        # Pass through voice summary for TTS
        voice_summary = tool_args.get("voice_summary")
        if voice_summary and isinstance(voice_summary, str):
            result["voice_summary"] = voice_summary
        return result

    if tool_name == "ignore":
        return {
            "type": "ignore",
            "reason": tool_args.get("reason", ""),
            "continue_listening": False,
        }

    command_name = tool_name
    command_params = dict(tool_args)
    if tool_name in _MUSIC_QUERY_TOOLS:
        raw_query = command_params.get("query")
        if isinstance(raw_query, str):
            command_params["query"] = _clean_music_query(raw_query)
    compound_actions = _COMPOUND_TOOLS.get(tool_name)
    if compound_actions is not None:
        selector_key = _COMPOUND_TOOL_SELECTOR_KEYS.get(tool_name, "action")
        action = command_params.pop(selector_key, None)
        if isinstance(action, str):
            command_name = compound_actions.get(action, tool_name)
        elif action is not None:
            command_params[selector_key] = action

    # Everything else remains a native-style tool call.
    return {
        "type": "tool_call",
        "tool": command_name,
        "args": command_params,
        "continue_listening": False,
    }
