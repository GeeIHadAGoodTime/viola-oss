# Privacy and data flow

Viola runs on your computer or on infrastructure you operate. This source
distribution does not require a Viola subscription or company account. The
`.env.example` profile selects a personal desktop, local phone mode, BYOK
(bring your own provider key), loopback API, local Whisper speech recognition,
and system speech output. These standalone paths do not require a company
account or company service. Cloud-capable code is included for features you
explicitly configure; its presence does not mean every cloud path runs in the
default profile.

## What stays local

The application stores preferences, conversation and memory files, encryption
material, and logs in the configured local data and log folders. Local models,
local Whisper, system TTS, local APIs, and local tool orchestration run on the
machine when selected. Keep the data folder, local API key, provider keys, and
logs under your control.

## What can leave the machine

The destination and payload depend on the feature and the settings you choose:

| Feature | Destination | Payload that may be sent | Default/control |
| --- | --- | --- | --- |
| Interactive AI | Your selected BYOK provider or local model | The prompt plus conversation, relevant memories, tool state, and other context assembled for that turn; this can include profile, identity, or address details present in that context | The example selects BYOK; choose Local to keep LLM inference local |
| Background memory selection | The configured background provider | The current query, recent tool names, and a manifest of available memory files | Background selection only runs when enabled and a usable provider key/source is configured; unsupported BYOK routing refuses shared-key fallback |
| Memory extraction and dream consolidation | The configured background provider | Extraction sends a bounded transcript after the source's sensitive-prompt redaction; dream consolidation sends bounded session-memory text after the source's redaction step | These background jobs require their respective memory settings and a usable provider key/source; they can create additional provider requests beyond the interactive turn |
| Context compaction | The configured background provider, when the conversation exceeds its context budget | An automatically selected older portion of the conversation, with runtime-only metadata filtered out and an input-size cap; it is summarized for continuation and is not described here as fully redacted | Compaction can happen automatically during a long conversation; provider/source availability controls whether summarization succeeds, with local fallback pruning when it does not |
| Cloud speech recognition | The configured cloud STT provider | Recorded speech audio and transcription parameters | Consent is required; the default is off and local Whisper is used without consent |
| Cloud speech synthesis | The configured TTS provider | Text selected for speech output | Select and configure a cloud TTS backend; system TTS is selected by the example |
| Carrier calls | Your configured carrier and your configured AI/speech providers | Call control details, audio, and transcripts needed for the call | The example selects local phone mode; self-hosted telephony requires your carrier account and credentials |
| Google Workspace connector | Google OAuth and the separately installed Workspace MCP process on your desktop | The restricted Google scopes you approved and the OAuth access/refresh tokens needed for those APIs | It is disabled unless you configure it and enable restricted Google features. The source permits its fixed local credential cache only for the active desktop user; it is unavailable on the shared cloud surface. |
| Web, browser, weather, music, vision, or other connectors | The provider or service you configure | The feature's request data, which may include query, page, clipboard, media, or account context | Use only the connectors you enable; consent gates apply to the relevant desktop cloud features |
| Optional sync, companion, diagnostics, replay, or error reporting | The configured relay or reporting provider | Feature-specific state or diagnostic data | Desktop cloud transmission defaults are opt-in; consent is required where implemented, and the source build has no company diagnostic relay configured by default |

Provider retention, training, access, and deletion policies are set by those
providers. Review their terms and configure endpoints you trust. The source
does not make a promise about provider handling.

## Debugging and local logs

`VIOLA_AI_DEBUG=true` enables the AI debug tracer. While enabled it can write
full questions, responses, metadata, and stage data to JSON files below the
configured log directory, and it also logs question/response prefixes. Treat
those files as raw conversational content: the ordinary durable-log redaction
policy does not reliably remove names or street addresses. Disable the flag
when finished and protect or remove the resulting files according to your own
retention policy.

## Tools and permissions

MCP servers, browser helpers, and shell programs run with the authority of your
operating-system user. Viola's environment filtering and permission parsers
help constrain requests, but they are not an operating-system sandbox. Review
and configure tools accordingly, and do not expose an unauthenticated local
API.

This is a technical description of the source paths and defaults. It is not a
legal privacy notice or a guarantee that a provider will retain no data.
