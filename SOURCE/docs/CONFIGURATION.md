# Configure your own providers

Copy `.env.example` to `.env` once. Environment variables configure secrets and
infrastructure; Settings in the application stores user preferences. Never put
provider secrets in version control or in bug reports.

The example uses `VIOLA_BUILD_PROFILE=personal`, `VIOLA_APP_SURFACE=desktop`,
`VIOLA_PHONE_MODE=local`, and `VIOLA_TTS_BACKEND=pyttsx3`. These settings select
the independent core and a system speech engine. `VIOLA_API_HOST=127.0.0.1`
keeps the API local to this computer.

## Connect your browser

Start Viola and open its local web address. The **Connect to your Viola** form
accepts the local API key that Viola creates at first startup. This is separate
from your AI provider key; no Viola account is required.

Open `secrets/initial_api_key` inside your Viola data folder and paste its contents
into **Local API key**, then select **Connect**. If you set `VIOLA_DATA_DIR`, that
is the data folder. A Windows source checkout otherwise uses `.viola` inside the
project; Linux and macOS use `$XDG_DATA_HOME/viola`, or `~/.local/share/viola` when
that variable is unset. Keep this key private: it authorizes access to your local
instance.

The form verifies the key with the protected local API before opening the
dashboard. It keeps the key only in the page's memory, so reloading the page
requires connecting again. The Qt desktop application supplies its local key
automatically. Hosted cloud installations retain their account sign-in flow.

## BYOK

BYOK means bring your own key. Select your provider in Settings and enter your
own API key. The provider bills your account directly. The example sets
`VIOLA_AI_SOURCE_OVERRIDE=byok`, which overrides a previously saved AI source.
Remove that override when you want the Settings selector to control the source.
Supported provider adapters are visible in Settings; an OpenAI-compatible provider
also needs its base URL and model name. No Viola login or company-managed key is
required for this path.

For an OpenAI-compatible server, enter its base URL, exact model name and provider
key, then choose **Save & use profile**. Select **Test active profile** to check
connectivity and tool support. This sends a small test request to that server.
The generic compatible adapter enables tool use only after the saved profile
passes that probe; repeat the test after changing its connection settings.

## Local models

Prepare a local inference server such as Ollama and download a model compatible
with its installed version. Change `VIOLA_AI_SOURCE_OVERRIDE=local` (or remove the
override and select Local in Settings). Set the local server URL and select the
exact installed model in the application. Ollama's conventional loopback endpoint
is `http://127.0.0.1:11434`. The model's hardware needs and license are independent
of Viola; choose an installed model that fits your machine.

## State and optional networks

The application keeps local preferences, history, encryption material and logs in
your user data folders. `VIOLA_DATA_DIR`, `VIOLA_CACHE_DIR` and `VIOLA_LOG_DIR`
can isolate another instance. Give that instance a distinct `VIOLA_API_PORT` too.
The local OS keyring or protected local fallback stores encryption material.
Local authentication uses SQLite. The company's PostgreSQL account implementation
is excluded; leave `VIOLA_DATABASE_URL` unset for this source distribution.

Local commands and local inference do not require company hosting. Features such
as web search, weather, browser automation, music streaming and carrier calls
contact their configured providers. Error reporting is unconfigured by default
in the source build; no company diagnostic relay is included. A local model may
need a one-time tokenizer/model download before it can operate offline.

For self-hosted calling, read [TELEPHONY.md](TELEPHONY.md). All available low-level
environment settings and their defaults are defined in `config/settings.py`.

## Independent video helper

The multiroom video widget defaults to the existing public `useviola.com` helper.
To serve it yourself, follow [the video helper guide](YOUTUBE_EMBED_HELPER.md).
Only the two independent helper sources from `ViolaWebsite/` are included; company
account, billing and marketing pages are excluded.
