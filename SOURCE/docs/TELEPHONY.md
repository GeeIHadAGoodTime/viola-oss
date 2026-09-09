# Run calls with your own providers

The independent source core can run the phone pipeline locally. You supply your
own Telnyx carrier account, calling number, connection and OpenAI API key. Provider
charges go to those accounts. A Viola company account is not required.

Set `VIOLA_PHONE_MODE=local`. If you have previously saved a phone-mode preference,
select Local in Settings too. Configure these environment settings in your private
`.env` file; do not put keys or stream URLs containing secrets into source control:

| Setting | Value |
|---|---|
| `VIOLA_TELNYX_API_KEY` | Your carrier API key |
| `VIOLA_TELNYX_PHONE_NUMBER` | Your outbound number in international E.164 format |
| `VIOLA_TELNYX_SIP_CONNECTION_ID` | Your Telnyx connection ID |
| `VIOLA_OPENAI_API_KEY` | Your speech/conversation provider key |
| `VIOLA_TELNYX_WEBHOOK_PUBLIC_KEY` | The carrier's Ed25519 public verification key |
| `VIOLA_TELNYX_PUBLIC_WEBHOOK_URL` | Public HTTPS URL ending `/webhooks/telnyx/local`, forwarded to the desktop API |
| `VIOLA_TELNYX_PUBLIC_WS_URL` | Public WSS URL forwarded to the phone media listener |
| `VIOLA_TELNYX_STREAM_SHARED_SECRET` | A fresh random secret shared only with your media configuration |

The default media listener is port 8770; the desktop API defaults to port 8756.
The transport appends the shared secret as a `token` query parameter automatically.
Expose only the callback and media paths through your HTTPS/WSS proxy or tunnel.
Keep the rest of the desktop API authenticated. Carrier callbacks require a valid,
fresh signature; media connections require the configured secret. An absent key,
unsigned callback or unowned call must fail before it can control a call.

The local pipeline supports one active media connection and queues subsequent
work. History and usage belong to the local authenticated owner; they are not a
company billing balance. The hosted edition retains its own billing and call
admission controls.

For speech output, install and configure the provider you select. Kokoro needs
the explicit [optional package and system eSpeak setup](INSTALLATION.md#optional-kokoro).
The `espeak` provider needs the system executable. The optional Piper provider
needs its own package and model; its GPL terms remain applicable. Unselected speech
providers are not prerequisites for local manager construction.

Use synthetic callbacks and a controlled local fixture before configuring a public
carrier route. A signed-callback or local-media test does not prove that your
carrier credentials, number, tunnel, audio hardware or real destination work.
Confirm those separately on your own installation before relying on calls.
